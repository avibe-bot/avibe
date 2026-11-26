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
        "direct path_use ingress session=1 role=owner minutes_ago=1 direct=1 tunnel=3",
        "direct path_use ingress session=2 role=member minutes_ago=1 direct=0 tunnel=1",
    ]
    # With no later request, a flush logs it; a session keeps its number, and
    # a minute is logged once.
    caplog.clear()
    clock.now += 60 * 4
    counts.flush()
    assert _lines(caplog) == ["direct path_use ingress session=2 role=member minutes_ago=4 direct=1 tunnel=0"]
    caplog.clear()
    counts.flush()
    clock.now += 600
    counts.flush()
    assert _lines(caplog) == []
    # Nothing of a session's identity is in a line.
    counts.note(owner, direct=True, owner=True)
    clock.now += 60
    counts.flush()
    assert _lines(caplog) == ["direct path_use ingress session=1 role=owner minutes_ago=1 direct=1 tunnel=0"]
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
    assert lines[0] == "direct path_use ingress session=0 role=several minutes_ago=1 direct=2 tunnel=1"
    assert lines[1] == "direct path_use ingress session=1 role=owner minutes_ago=1 direct=2 tunnel=0"
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
    # Through the tunnel: the rendezvous request itself is one.
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
        "direct path_use ingress session=1 role=owner minutes_ago=1 direct=1 tunnel=2",
        "direct path_use ingress session=2 role=member minutes_ago=1 direct=0 tunnel=1",
    ]


def test_a_later_request_of_the_session_logs_the_minutes_that_are_over(instance, counted, caplog):  # noqa: F811
    config, _rendezvous = instance
    clock, _counts = counted
    caplog.set_level("INFO", logger="vibe.direct_path_use")
    client = _signed_in(config)
    assert _post(client, _body()).status_code == 200
    clock.now += 120
    assert _post(client, _body()).status_code == 200
    assert _lines(caplog) == ["direct path_use ingress session=1 role=member minutes_ago=2 direct=0 tunnel=1"]
