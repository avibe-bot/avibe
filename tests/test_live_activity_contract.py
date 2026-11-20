"""Contract vectors for the iOS Live Activity of one Turn (I5).

The contract is docs/plans/2026-11-20-ios-live-activity-contract.md; the vectors
are tests/fixtures/live_activity/contract_vectors.json and are shared byte for
byte with the relay (avibe-backend) and the iOS tests. This module holds a
reference of the two rules every side must agree on: which relay request bodies
are valid, and the exact APNs request the relay builds from a valid one. The
relay implements the same rules in TypeScript; the iOS tests decode the
``content-state`` of every APNs body into ``TurnActivityAttributes.ContentState``.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

import pytest

VECTORS = Path(__file__).parent / "fixtures" / "live_activity" / "contract_vectors.json"

PHASES_UPDATE = {"working", "needsYou"}
PHASES_END = {"done", "failed", "stopped"}
REQUIRED = {"activity_token", "event", "timestamp", "priority", "phase", "started_at", "confirmed_at"}
OPTIONAL = {"ended_at", "stale_at", "dismiss_at", "alert"}
MIN_TIME = 1_600_000_000
MAX_TIME = 4_102_444_800
TOKEN_RE = re.compile(r"(?:[0-9a-f]{2}){8,256}")  # matched with fullmatch()
UPDATE_EXPIRATION_SECONDS = 3600
END_EXPIRATION_SECONDS = 14_400
ALERT = {
    "title": {"loc-key": "AVIBE_LIVE_ACTIVITY_NEEDS_YOU_TITLE"},
    "body": {"loc-key": "AVIBE_LIVE_ACTIVITY_NEEDS_YOU_BODY"},
    "sound": "default",
}


MAX_BODY_BYTES = 1024


def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def validate_relay_raw(raw: bytes) -> bool:
    """True when the raw request body is valid (section 3.2): the size limit and the decoding rules
    are checked on the bytes, before and while parsing, then the parsed object is validated."""
    if len(raw) > MAX_BODY_BYTES:
        return False
    try:
        # Strict UTF-8 with no BOM stripping: a leading BOM stays in the text and json rejects it.
        body = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError):
        return False
    return validate_relay_request(body)


def _time(value: Any) -> bool:
    return type(value) is int and MIN_TIME <= value <= MAX_TIME


def validate_relay_request(body: Any) -> bool:
    """True when ``body`` is a valid ``/live-activity`` request body (section 3.2)."""
    if not isinstance(body, dict):
        return False
    keys = set(body)
    if not REQUIRED <= keys or not keys <= REQUIRED | OPTIONAL:
        return False
    if not all(isinstance(body[k], str) for k in ("activity_token", "event", "phase")):
        return False
    if "alert" in body and not isinstance(body["alert"], str):
        return False
    if not TOKEN_RE.fullmatch(body["activity_token"]):
        return False
    if type(body["priority"]) is not int or body["priority"] not in (5, 10):
        return False
    times = [body[k] for k in ("timestamp", "started_at", "confirmed_at", "ended_at", "stale_at", "dismiss_at") if k in body]
    if not all(_time(t) for t in times):
        return False
    if body["started_at"] > body["confirmed_at"]:
        return False
    event, phase = body["event"], body["phase"]
    if event == "update":
        if phase not in PHASES_UPDATE or "stale_at" not in body:
            return False
        if "ended_at" in body or "dismiss_at" in body:
            return False
        if body["stale_at"] <= body["confirmed_at"]:
            return False
        if "alert" in body and (body["alert"] != "needsYou" or phase != "needsYou" or body["priority"] != 10):
            return False
        return True
    if event == "end":
        if phase not in PHASES_END or "ended_at" not in body or "dismiss_at" not in body:
            return False
        if "stale_at" in body or "alert" in body:
            return False
        return body["started_at"] <= body["ended_at"] <= body["dismiss_at"]
    return False


def build_apns_request(body: dict[str, Any], *, installation_topic: str) -> dict[str, Any]:
    """The exact APNs request the relay sends for a valid body (section 3.3)."""
    state: dict[str, Any] = {"phase": body["phase"], "startedAt": body["started_at"]}
    if "ended_at" in body:
        state["endedAt"] = body["ended_at"]
    state["confirmedAt"] = body["confirmed_at"]
    aps: dict[str, Any] = {"timestamp": body["timestamp"], "event": body["event"], "content-state": state}
    if body["event"] == "update":
        aps["stale-date"] = body["stale_at"]
        expiration = body["timestamp"] + UPDATE_EXPIRATION_SECONDS
    else:
        aps["dismissal-date"] = body["dismiss_at"]
        expiration = body["timestamp"] + END_EXPIRATION_SECONDS
    if body.get("alert") == "needsYou":
        aps["alert"] = ALERT
    return {
        "path": f"/3/device/{body['activity_token']}",
        "headers": {
            "apns-push-type": "liveactivity",
            "apns-topic": f"{installation_topic}.push-type.liveactivity",
            "apns-priority": str(body["priority"]),
            "apns-expiration": str(expiration),
        },
        "body_utf8": json.dumps({"aps": aps}, ensure_ascii=False, separators=(",", ":")),
    }


@pytest.fixture(scope="module")
def vectors() -> dict[str, Any]:
    return json.loads(VECTORS.read_text(encoding="utf-8"))


def test_every_relay_case_is_valid_and_maps_to_its_exact_apns_request(vectors):
    for case in vectors["relay_cases"]:
        assert validate_relay_request(case["request"]), case["name"]
        built = build_apns_request(case["request"], installation_topic=vectors["installation_topic"])
        assert built == case["apns"], case["name"]


def _compact(body: Any) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def test_every_rejection_is_invalid(vectors):
    for case in vectors["relay_rejections"]:
        assert not validate_relay_request(case["request"]), case["name"]
        assert not validate_relay_raw(_compact(case["request"])), case["name"]
        assert (case["status"], case["error"]) == (400, "invalid_live_activity")


def test_raw_bodies_are_judged_on_their_bytes(vectors):
    for case in vectors["relay_cases"]:
        assert validate_relay_raw(_compact(case["request"])), case["name"]
    for case in vectors["relay_raw_accepts"]:
        raw = base64.b64decode(case["body_base64"])
        assert validate_relay_raw(raw), case["name"]
    for case in vectors["relay_raw_rejections"]:
        raw = base64.b64decode(case["body_base64"])
        assert not validate_relay_raw(raw), case["name"]
        assert (case["status"], case["error"]) == (400, "invalid_live_activity")


WRONG_TYPES = [None, True, 1.5, 7, "", "x", [], ["working"], {}, {"phase": "working"}]


def test_validator_answers_false_for_any_wrong_type_in_any_field(vectors):
    # Malformed input is a 400, never a server error: substitute every JSON type into every field of
    # every valid case and require a plain boolean back.
    for case in vectors["relay_cases"]:
        for key in case["request"]:
            for value in WRONG_TYPES:
                body = {**case["request"], key: value}
                result = validate_relay_request(body)
                assert isinstance(result, bool), (case["name"], key, value)
                if type(value) is not type(case["request"][key]) or isinstance(value, bool):
                    assert result is False, (case["name"], key, value)


def test_content_state_carries_no_text_beyond_the_phase(vectors):
    # The relay and Apple see the state in the clear: only the phase enum and Unix seconds.
    for case in vectors["relay_cases"]:
        state = json.loads(case["apns"]["body_utf8"])["aps"]["content-state"]
        assert set(state) <= {"phase", "startedAt", "endedAt", "confirmedAt"}
        assert state["phase"] in PHASES_UPDATE | PHASES_END
        assert all(type(state[k]) is int for k in state if k != "phase")


def test_runtime_examples_match_the_relay_and_ios_shapes(vectors):
    api = vectors["runtime_api"]
    activity = api["put_response"]["activity"]
    assert set(activity) == {
        "activity_id", "session_id", "turn_id", "phase", "started_at", "ended_at", "confirmed_at", "status",
    }
    assert api["put_request"]["session_id"] == activity["session_id"] == vectors["bridge"]["start"]["sessionId"]
    assert api["put_request"]["activity_token"] == vectors["relay_cases"][0]["request"]["activity_token"]
    ended = api["turn_not_active"]["activity"]
    assert ended["phase"] in PHASES_END and ended["status"] == "ended" and ended["ended_at"] is not None
    for outcome, phase in vectors["phase_map"].items():
        assert outcome in {"completed", "failed", "not_written", "canceled"}
        assert phase in PHASES_END
    attributes = vectors["ios_attributes"]
    assert attributes["agentName"] == vectors["bridge"]["start"]["agentName"]
    assert attributes["sessionTitle"] == vectors["bridge"]["start"]["sessionTitle"]
