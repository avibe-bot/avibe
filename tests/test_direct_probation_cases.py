"""The probation cases both shells run (``docs/plans/remote-direct-probation.md``).

The JSON is written by the reference model beside it. These tests hold three
things: the file is what the model writes today; the numbers the rule was
agreed for are in it, typed here and not read from the model; and the file
keeps the shape the two runners (Swift and Rust) were written against.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
CASES = FIXTURES / "direct_probation_cases.json"


def _model():
    spec = importlib.util.spec_from_file_location("make_direct_probation_cases", FIXTURES / "make_direct_probation_cases.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cases() -> dict[str, dict]:
    return {case["name"]: case for case in json.loads(CASES.read_text(encoding="utf-8"))["cases"]}


def _last(case: dict, key: str = "a1/i1") -> dict:
    return [event["expect"] for event in case["events"] if event.get("key") == key and "expect" in event][-1]


def test_the_file_is_what_the_model_writes():
    assert CASES.read_text(encoding="utf-8") == _model().render(), (
        "run: python tests/fixtures/make_direct_probation_cases.py"
    )


def test_the_agreed_numbers_are_in_the_file():
    cases = _cases()
    data = json.loads(CASES.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["rule"] == {"levels_s": [0, 30, 600], "early_ms": 600_000, "held_ms": 600_000, "expiry_ms": 1_800_000}

    # A path that dies every 20 s is adopted once in half an hour; one that
    # dies every 3 minutes, twice in an hour. Both end on the longest wait.
    flap_20s = cases["dies_every_20s"]
    assert flap_20s["events"][-1]["at_ms"] == 1_800_000
    assert (_last(flap_20s)["adoptions"], _last(flap_20s)["level_s"], _last(flap_20s)["usable"]) == (1, 600, False)
    flap_3min = cases["dies_every_3min"]
    assert flap_3min["events"][-1]["at_ms"] == 3_600_000
    assert (_last(flap_3min)["adoptions"], _last(flap_3min)["level_s"], _last(flap_3min)["usable"]) == (2, 600, False)

    # A healthy path is used at link-up.
    first_up = next(event for event in cases["healthy"]["events"] if event["event"] == "link_up")
    assert first_up["expect"]["wait_ms"] == 0
    assert (_last(cases["healthy"])["adoptions"], _last(cases["healthy"])["level_s"]) == (1, 0)

    # In and out every minute never moves the level, and every return is adopted at once.
    returns = cases["five_returns_a_minute_apart"]
    keyed = [event["expect"] for event in returns["events"] if "expect" in event]
    assert {expect["level_s"] for expect in keyed} == {0}
    assert {expect["rose"] for expect in keyed} == {"none"}
    assert [e["expect"]["wait_ms"] for e in returns["events"] if e["event"] == "link_up"] == [0] * 5
    assert _last(returns)["adoptions"] == 5

    # Our own closes never raise it.
    for cause in ("background", "choice", "superseded", "instance_ended", "network_change"):
        assert _last(cases[f"own_close_{cause}"])["level_s"] == 0, cause

    # The level survives a relaunch, and the time away counts towards its expiry.
    assert _last(cases["relaunch_keeps_the_level"])["level_s"] == 30
    assert _last(cases["relaunch_after_expiry"])["level_s"] == 0


def test_the_file_keeps_the_shape_the_runners_read():
    data = json.loads(CASES.read_text(encoding="utf-8"))
    kinds = {"foreground", "background", "relaunch", "clock_back", "link_up", "adopt", "link_down", "check"}
    keyed = {"link_up", "adopt", "link_down", "check"}
    names = [case["name"] for case in data["cases"]]
    assert len(names) == len(set(names))
    for case in data["cases"]:
        assert set(case) == {"name", "about", "milestones", "events"}
        at = 0
        for event in case["events"]:
            assert event["event"] in kinds, event
            assert isinstance(event["at_ms"], int) and event["at_ms"] >= at, event
            at = event["at_ms"]
            if event["event"] in keyed:
                expect = event["expect"]
                assert expect["level_s"] in data["rule"]["levels_s"]
                assert expect["rose"] in {"none", "adopted", "probation"}
                assert (expect["level_s"] == 0) == (expect["rose"] == "none"), event
                assert isinstance(expect["usable"], bool) and isinstance(expect["adoptions"], int)
                assert ("wait_ms" in expect) == (event["event"] == "link_up")
                assert ("adopted" in expect) == (event["event"] == "adopt")
            else:
                assert "expect" not in event and "key" not in event
            if event["event"] == "link_down":
                assert event["cause"] in data["causes"]
            if event["event"] == "clock_back":
                assert event["by_ms"] > 0
        for mark in case["milestones"]:
            assert set(mark) == {"at_ms", "key", "adoptions", "level_s"}
