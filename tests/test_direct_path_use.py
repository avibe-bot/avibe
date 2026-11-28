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


def test_hourly_changes_compare_majorities_across_real_gaps_and_charge_the_later_hour(caplog):
    clock = _Clock()
    counts = IngressCounts(clock)
    caplog.set_level("INFO", logger="vibe.direct_path_use")

    def note(at, direct, session="private-session"):
        clock.now = at
        counts.note(session, direct=direct, owner=False)

    # Single-request minutes qualify. An unchanged strict majority is not a change.
    note(3500, True)
    note(3550, False)
    note(3551, True)
    note(3552, True)
    # 90s between actual arrivals, despite the absent minute; charge to hour 1.
    note(3642, False)
    note(3660, True)
    # A >90s silence before a tied minute is still a gap at the next vote.
    note(3760, False)
    note(3761, True)
    note(3780, False)
    # Another session's traffic cannot bridge this gap.
    note(3840, False, "another-private-session")
    note(3870.01, True)
    clock.now = 10800
    counts.flush()
    counts.flush()
    assert [line for line in _lines(caplog) if " hourly " in line] == [
        "direct path_use hourly session=1 hours_ago=1 to_direct=0 to_other=0 gap_to_direct=0 gap_to_other=0",
        "direct path_use hourly session=1 hours_ago=2 to_direct=1 to_other=1 gap_to_direct=1 gap_to_other=1",
        "direct path_use hourly session=2 hours_ago=2 to_direct=0 to_other=0 gap_to_direct=0 gap_to_other=0",
    ]


def test_hourly_counts_do_not_compare_pooled_or_forgotten_sessions(monkeypatch, caplog):
    clock = _Clock()
    counts = IngressCounts(clock)
    caplog.set_level("INFO", logger="vibe.direct_path_use")
    monkeypatch.setattr(direct_path_use, "_MAX_SESSIONS_PER_MINUTE", 1)
    monkeypatch.setattr(direct_path_use, "_MAX_SESSIONS", 2)
    counts.note("a", direct=True, owner=True)
    clock.now += 60
    counts.note("b", direct=False, owner=True)
    counts.note("a", direct=False, owner=True)  # pooled: break comparison for a only
    clock.now += 60
    counts.note("a", direct=False, owner=True)
    clock.now += 60
    counts.note("c", direct=True, owner=True)  # b forgotten
    clock.now += 60
    counts.note("b", direct=True, owner=True)  # new number, no comparison
    clock.now = 3600
    counts.flush()
    assert [line for line in _lines(caplog) if " hourly " in line] == [
        f"direct path_use hourly session={n} hours_ago=1 to_direct=0 to_other=0 gap_to_direct=0 gap_to_other=0"
        for n in (1, 2, 3, 4)
    ]


def test_many_sessions_in_one_minute_share_one_line_past_the_bound(caplog):
    clock = _Clock()
    counts = IngressCounts(clock)
    caplog.set_level("INFO", logger="vibe.direct_path_use")
    bound = direct_path_use._MAX_SESSIONS_PER_MINUTE
    for index in range(bound + 3):
        counts.note(("sub", index), direct=index % 2 == 0, owner=index == 0,
                    page=("asked", 5) if index >= bound else None)
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


@pytest.mark.parametrize("word", [
    "no_path", "on_path", "not_ready", "verdict", "spacing", "cap", "quiet_request",
    "quiet_socket", "quiet_frame", "not_ours", "asked", "idle",
])
def test_page_report_accepts_only_a_bounded_optional_hold(word):
    assert direct_path_use.page_sample([word]) == (word, None)
    for ms in (0, 1, 32, 999, 1000):
        assert direct_path_use.page_sample([f"{word};hold={ms}"]) == (word, ms)


@pytest.mark.parametrize("fields", [
    [], [""], ["arbitrary;hold=7"], ["Asked"], ["asked "], ["asked; hold=1"],
    ["asked;hold="], ["asked;hold=00"], ["asked;hold=012"], ["asked;hold=+1"],
    ["asked;hold=-1"], ["asked;hold=１"], ["asked;hold=1001"], ["asked;hold=1.0"],
    ["asked;hold=1;hold=2"], ["asked;extra=1"], ["asked;hold=1\n"], ["asked, idle"],
    ["asked", "asked"], ["asked;hold=" + "9" * 5000],
])
def test_bad_page_field_discards_the_whole_sample(fields):
    assert direct_path_use.page_sample(fields) is None


def test_page_fields_follow_only_accepted_api_requests_in_the_same_session_minute(instance, counted, caplog):  # noqa: F811
    config, _ = instance
    clock, counts = counted
    caplog.set_level("INFO", logger="vibe.direct_path_use")
    owner = _signed_in(config, role="owner", subject="owner")
    viewer = _signed_in(config)

    def get(client, path, word, **more_headers):
        answer = client.get(path, base_url=ORIGIN, environ_base=REMOTE_PEER,
                            headers={"X-Avibe-Page": word, **more_headers})
        assert "X-Avibe-Page" not in answer.headers
        return answer

    assert get(owner, "/api/inbox", "quiet_request;hold=1000").status_code == 200
    assert get(viewer, "/api/session", "idle;hold=0").get_json()["authenticated"] is True
    assert get(owner, "/api/session", "asked;hold=2").get_json()["authenticated"] is True
    assert get(owner, "/api/session", "asked").status_code == 200
    assert get(owner, "/api/session", "verdict;hold=1001").status_code == 200
    # Two differently cased keys survive the test wrapper's ordinary dict and
    # reach ASGI as two fields. A comma-coalesced equivalent is invalid too.
    assert get(owner, "/api/session", "on_path", **{"x-avibe-page": "cap"}).status_code == 200
    assert get(app.test_client(), "/api/inbox", "not_ours").status_code == 401
    assert get(owner, "/health", "spacing").status_code == 200
    clock.now += 60
    assert get(owner, "/api/session", "on_path").status_code == 200
    clock.now += 60
    counts.flush()
    assert _lines(caplog) == [
        "direct path_use ingress session=1 role=owner minutes_ago=1 direct=0 other=5"
        " page_last=asked page_seen=asked,quiet_request hold_count=2 hold_max_ms=1000",
        "direct path_use ingress session=2 role=member minutes_ago=1 direct=0 other=1"
        " page_last=idle page_seen=idle hold_count=1 hold_max_ms=0",
        "direct path_use ingress session=1 role=owner minutes_ago=1 direct=0 other=1"
        " page_last=on_path page_seen=on_path hold_count=0",
    ]


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


def test_only_an_answer_no_cache_may_keep_says_how_its_request_arrived(
    instance, counted, monkeypatch, tmp_path  # noqa: F811
):
    """Given again after the client changed paths, or by a shared cache to
    another client, the word would be about a request the instance never saw."""
    config, _rendezvous = instance
    (tmp_path / "logo-unread.png").write_bytes(b"\x89PNG\r\n")
    monkeypatch.setattr(ui_server, "get_ui_dist_path", lambda: tmp_path)
    owner = _signed_in(config, role="owner", subject="sub-owner")
    header = ui_server.REQUEST_VIA_HEADER

    def get(path):
        return owner.get(path, base_url=ORIGIN, environ_base=REMOTE_PEER)

    with monkeypatch.context() as patch:
        patch.setattr(ui_server, "_is_direct_ingress_request", lambda: True)
        # A static file with a lifetime, served to the signed-in session.
        asset = get("/logo-unread.png")
        assert asset.status_code == 200 and "max-age" in asset.headers["Cache-Control"]
        assert header not in asset.headers
        # An ordinary gated API answer now gets the default before the word is decided.
        plain = get("/api/inbox")
        assert plain.status_code == 200 and plain.headers["Cache-Control"] == "private, no-store"
        assert plain.headers.get(header) == "direct"
        # The page's session check says no-store, and says the word.
        check = get("/api/session")
        assert "no-store" in check.headers["Cache-Control"]
        assert check.headers.get(header) == "direct"


@pytest.mark.parametrize(
    ("cache_control", "never_stored"),
    [
        ("no-store", True),
        ("no-store, private", True),
        ("private, No-Store", True),
        (None, False),
        ("", False),
        ("no-cache", False),
        ("private, max-age=31536000, immutable", False),
        ("public, max-age=3600", False),
        # A Show runtime's own policy, forwarded. Nothing with a quoted
        # string is read at all: its text is an argument, may hold commas,
        # and may hold an escaped quote, so a "no-store" inside it is
        # extension text to a cache.
        ('private, max-age=3600, no-cache="Set-Cookie"', False),
        ('private, no-cache="no-store"', False),
        ('private, max-age=3600, ext="a\\", no-store, \\"b"', False),
        ('no-store, ext="a"', False),
        ("max-age=3600, ext=a\\, no-store", False),
        # Only the whole token, by itself.
        ("x-no-store", False),
        ("no-store-x", False),
        ("no-store=1", False),
        ("max-age=3600; no-store", False),
        ("private,\tno-store ", True),
    ],
)
def test_an_answer_is_never_stored_only_by_its_own_directive(cache_control, never_stored):
    headers = {} if cache_control is None else {"Cache-Control": cache_control}
    assert ui_server._answer_is_never_stored(ui_server.Response(b"", headers=headers)) is never_stored


@pytest.mark.parametrize(
    ("lines", "never_stored"),
    [
        # A cache reads several lines as one list.
        (["private", "no-store"], True),
        (["max-age=3600", "no-store"], True),
        # A quoted string that begins on one line and ends on another: the
        # line between them is its text, whichever line is looked at.
        (['max-age=3600, ext="a', "no-store", 'b"'], False),
        (["no-store", 'max-age=3600, ext="a"'], False),
    ],
)
def test_every_cache_control_line_of_an_answer_is_read_as_one_list(lines, never_stored):
    response = ui_server.Response(b"")
    for line in lines:
        response.headers.append("Cache-Control", line)
    assert ui_server._answer_is_never_stored(response) is never_stored


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
