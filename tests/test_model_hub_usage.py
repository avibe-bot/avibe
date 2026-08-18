"""Model Hub usage metering: wire extraction and bounded persistence.

Two properties are under test. Extraction must read exactly what each protocol
reports and refuse everything else, because token counts are the one number in
the hub that a hostile upstream fully controls. Persistence must stay bounded and
must survive an older or corrupt file, because the ledger is a shipped on-disk
surface.

Gateway recording is covered in ``test_model_hub_l3.py``, next to the turn
settlement scaffolding it depends on.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.handlers.model_hub import state_file
from core.handlers.model_hub.identifiers import MODEL_ID_MAX_LENGTH
from core.handlers.model_hub.stream_wire import (
    PROTOCOL_STREAM_TAXONOMY,
    USAGE_TOKEN_CEILING,
    ProtocolSSEState,
    ProtocolUsageReport,
    extract_protocol_usage,
    observe_protocol_response,
)
from core.handlers.model_hub.usage import (
    USAGE_RETENTION_DAYS,
    BoundedUsageLedger,
    local_usage_day,
)

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


def _frame(document: dict) -> bytes:
    """Render one SSE frame the way the protocols that name events do.

    Anthropic and OpenAI Responses both send `event:` alongside the discriminator
    in the body, and both are required to match a terminal envelope. OpenAI Chat
    sends unnamed frames, which is exactly the shape a body without `type` gets.
    """

    named = f"event: {document['type']}\n".encode("utf-8") if "type" in document else b""
    return named + b"data: " + json.dumps(document).encode("utf-8") + b"\n\n"


def _frames(*documents: dict) -> tuple[bytes, ...]:
    return tuple(_frame(document) for document in documents)


def _observed(protocol: str, *documents: dict) -> ProtocolUsageReport | None:
    state = ProtocolSSEState(protocol)
    for frame in _frames(*documents):
        state.observe(frame)
    return state.usage


# --- Extraction: every protocol declares where it reports usage ---------------


def test_every_observed_protocol_declares_a_usage_location() -> None:
    """A new protocol cannot ship unmetered: the table demands the location."""

    for protocol, taxonomy in PROTOCOL_STREAM_TAXONOMY.items():
        usage = taxonomy.usage
        assert usage.container_paths, protocol
        assert usage.input_paths, protocol
        assert usage.output_paths, protocol
        assert usage.cached_input_paths, protocol


def test_anthropic_input_total_sums_the_cache_members() -> None:
    """Anthropic reports `input_tokens` net of cache, so input is the sum."""

    report = extract_protocol_usage(
        "anthropic",
        {
            "type": "message",
            "usage": {
                "input_tokens": 12,
                "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 88,
                "output_tokens": 40,
            },
        },
    )

    assert report == ProtocolUsageReport(
        input_tokens=1000,
        cached_input_tokens=900,
        output_tokens=40,
    )


def test_anthropic_stream_keeps_the_largest_cumulative_report() -> None:
    """`message_delta` restates a running total, so the largest frame wins."""

    usage = _observed(
        "anthropic",
        {"type": "message_start", "message": {"usage": {"input_tokens": 1000, "output_tokens": 1}}},
        {"type": "content_block_delta", "delta": {"text": "hi"}},
        {"type": "message_delta", "usage": {"output_tokens": 40}},
        {"type": "message_delta", "usage": {"output_tokens": 250}},
        {"type": "message_stop"},
    )

    assert usage == ProtocolUsageReport(input_tokens=1000, output_tokens=250)


def test_a_replayed_usage_frame_cannot_double_count() -> None:
    """Merging by the larger value makes retries and duplicates harmless."""

    single = _observed(
        "openai_responses",
        {"type": "response.in_progress", "response": {"usage": {"input_tokens": 700, "output_tokens": 9}}},
    )
    replayed = _observed(
        "openai_responses",
        {"type": "response.in_progress", "response": {"usage": {"input_tokens": 700, "output_tokens": 9}}},
        {"type": "response.in_progress", "response": {"usage": {"input_tokens": 700, "output_tokens": 9}}},
    )

    assert single == replayed == ProtocolUsageReport(input_tokens=700, output_tokens=9)


def test_openai_responses_reports_cached_input_as_a_subset() -> None:
    """`input_tokens` already includes cached input, so it is never an addend."""

    usage = _observed(
        "openai_responses",
        {
            "type": "response.completed",
            "response": {
                "usage": {
                    "input_tokens": 5000,
                    "input_tokens_details": {"cached_tokens": 4096},
                    "output_tokens": 77,
                }
            },
        },
    )

    assert usage == ProtocolUsageReport(
        input_tokens=5000,
        cached_input_tokens=4096,
        output_tokens=77,
    )


def test_usage_on_the_terminal_frame_is_observed_before_settlement() -> None:
    """The frame that ends the stream is also the one that carries the report."""

    state = ProtocolSSEState("openai_responses")
    for frame in _frames(
        {"type": "response.completed", "response": {"usage": {"input_tokens": 30, "output_tokens": 4}}},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 999999, "output_tokens": 4}}},
    ):
        state.observe(frame)

    assert state.terminal_outcome == "served"
    # The second frame arrives after settlement and is ignored entirely, so it
    # cannot inflate the report either.
    assert state.usage == ProtocolUsageReport(input_tokens=30, output_tokens=4)


def test_a_terminal_error_carries_the_tokens_reported_before_it() -> None:
    """Review 4959575659 finding 7: Anthropic bills input on `message_start`.

    The terminal observation is what the runtime turns into a bodyless outcome for
    resolver metering, so dropping the accumulated report there would lose tokens
    on exactly the streams that end badly.
    """

    state = ProtocolSSEState("anthropic")
    for frame in _frames(
        {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 900, "cache_read_input_tokens": 128}},
        },
        {"type": "error", "error": {"type": "overloaded_error"}},
    ):
        state.observe(frame)

    observation = state.terminal_observation()
    assert observation is not None
    assert observation.outcome != "served"
    assert observation.usage == ProtocolUsageReport(
        input_tokens=1028,
        cached_input_tokens=128,
    )


def test_openai_chat_reports_usage_only_on_its_final_chunk() -> None:
    """Chat streaming carries usage on a dedicated chunk, then `[DONE]`."""

    state = ProtocolSSEState("openai_chat")
    state.observe(_frames({"choices": [{"delta": {"content": "hi"}}]})[0])
    assert state.usage is None

    state.observe(
        _frames(
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 610,
                    "prompt_tokens_details": {"cached_tokens": 512},
                    "completion_tokens": 22,
                },
            }
        )[0]
    )
    state.observe(b"data: [DONE]\n\n")

    assert state.terminal_outcome == "served"
    assert state.usage == ProtocolUsageReport(
        input_tokens=610,
        cached_input_tokens=512,
        output_tokens=22,
    )


def test_a_buffered_response_without_a_report_stays_unmetered() -> None:
    """A missing report is absent, never zero: the caller tracks the shortfall."""

    observation = observe_protocol_response(
        "openai_chat",
        streamed=False,
        data=json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode("utf-8"),
    )

    assert observation.outcome == "served"
    assert observation.usage is None


def test_a_buffered_error_still_carries_the_tokens_it_billed() -> None:
    """A vendor that reported tokens billed us even when the turn failed."""

    observation = observe_protocol_response(
        "anthropic",
        streamed=False,
        data=json.dumps(
            {
                "error": {"type": "overloaded_error"},
                "usage": {"input_tokens": 41, "output_tokens": 0},
            }
        ).encode("utf-8"),
    )

    assert observation.outcome == "failed_terminal"
    assert observation.usage == ProtocolUsageReport(input_tokens=41)


@pytest.mark.parametrize(
    "reported",
    [
        pytest.param(-1, id="negative"),
        pytest.param(True, id="bool"),
        pytest.param("120", id="string"),
        pytest.param(12.5, id="float"),
        pytest.param(None, id="null"),
        pytest.param([120], id="list"),
        pytest.param(USAGE_TOKEN_CEILING + 1, id="above-ceiling"),
    ],
)
def test_an_unusable_token_value_is_dropped_not_coerced(reported: object) -> None:
    """Only a bounded non-negative integer count is a token count."""

    report = extract_protocol_usage(
        "openai_chat",
        {"usage": {"prompt_tokens": reported, "completion_tokens": 7}},
    )

    assert report == ProtocolUsageReport(output_tokens=7)


def test_a_composed_input_total_cannot_exceed_our_own_ceiling() -> None:
    """The ceiling lives in our code, never in a total the response declares."""

    report = extract_protocol_usage(
        "anthropic",
        {
            "usage": {
                "input_tokens": USAGE_TOKEN_CEILING,
                "cache_read_input_tokens": USAGE_TOKEN_CEILING,
                "total_tokens": 3,
            }
        },
    )

    assert report is not None
    assert report.input_tokens == USAGE_TOKEN_CEILING
    assert report.cached_input_tokens == USAGE_TOKEN_CEILING


@pytest.mark.parametrize(
    "reported_input,reported_cached,expected_cached",
    [
        pytest.param(100, 4096, 100, id="cached-above-input"),
        pytest.param(0, 4096, 0, id="cached-without-input"),
        pytest.param(4096, 4096, 4096, id="fully-cached"),
    ],
)
def test_cached_input_never_exceeds_the_input_it_is_a_part_of(
    reported_input: int,
    reported_cached: int,
    expected_cached: int,
) -> None:
    """The subset the read contract promises is bounded by our own input count."""

    report = extract_protocol_usage(
        "openai_responses",
        {
            "usage": {
                "input_tokens": reported_input,
                "input_tokens_details": {"cached_tokens": reported_cached},
                "output_tokens": 7,
            }
        },
    )

    assert report == ProtocolUsageReport(
        input_tokens=reported_input,
        cached_input_tokens=expected_cached,
        output_tokens=7,
    )


def test_the_subset_holds_when_two_frames_each_win_one_field() -> None:
    """Per-field max can compose a cached count larger than its own input."""

    usage = _observed(
        "openai_responses",
        {
            "type": "response.in_progress",
            "response": {
                "usage": {
                    "input_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 8},
                    "output_tokens": 1,
                }
            },
        },
        {
            "type": "response.completed",
            "response": {
                "usage": {
                    "input_tokens": 12,
                    "input_tokens_details": {"cached_tokens": 4096},
                    "output_tokens": 3,
                }
            },
        },
    )

    assert usage == ProtocolUsageReport(
        input_tokens=12,
        cached_input_tokens=12,
        output_tokens=3,
    )


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"type": "ping"}, id="no-container"),
        pytest.param({"usage": "many"}, id="container-not-an-object"),
        pytest.param({"usage": {}}, id="empty-container"),
        pytest.param({"usage": {"unknown_tokens": 5}}, id="unknown-members-only"),
    ],
)
def test_a_document_with_no_readable_report_yields_none(payload: dict) -> None:
    assert extract_protocol_usage("openai_chat", payload) is None


# --- Persistence: bounded, credential-free, and safe to load -----------------


class _Clock:
    """The moment a write happens, which a test moves independently of `at`.

    They are the same clock read at two times, and the gap between them is the
    point: `at` is captured when a call ends, this is read when its row is
    persisted, and metering runs off the event loop in between.
    """

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


def _ledger(tmp_path: Path, **kwargs) -> BoundedUsageLedger:
    kwargs.setdefault("now", _Clock(NOW + timedelta(hours=12)))
    return BoundedUsageLedger(tmp_path / "state" / "usage.json", **kwargs)


def test_records_fold_into_one_row_per_day_source_and_model(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    ledger.record(
        source_id="src_a",
        model_id="model-x",
        usage=ProtocolUsageReport(input_tokens=100, cached_input_tokens=40, output_tokens=7),
        at=NOW,
    )
    ledger.record(
        source_id="src_a",
        model_id="model-x",
        usage=ProtocolUsageReport(input_tokens=200, output_tokens=3),
        at=NOW + timedelta(hours=1),
    )

    rows = ledger.window(days=1, now=NOW + timedelta(hours=1))
    assert len(rows) == 1
    assert rows[0]["requests"] == 2
    assert rows[0]["token_reports"] == 2
    assert rows[0]["input_tokens"] == 300
    assert rows[0]["cached_input_tokens"] == 40
    assert rows[0]["output_tokens"] == 7 + 3
    assert rows[0]["last_metered_at"] == (NOW + timedelta(hours=1)).isoformat()


def test_a_turn_without_a_report_counts_the_request_only(tmp_path: Path) -> None:
    """`requests` is self-measured; a missing report must not read as zero usage."""

    ledger = _ledger(tmp_path)

    ledger.record(source_id="src_a", model_id="model-x", usage=None, at=NOW)
    ledger.record(
        source_id="src_a",
        model_id="model-x",
        usage=ProtocolUsageReport(input_tokens=90),
        at=NOW,
    )

    totals = ledger.summary(days=7, now=NOW)["totals"]
    assert totals["requests"] == 2
    assert totals["token_reports"] == 1
    assert totals["input_tokens"] == 90


def test_summary_groups_by_source_then_model_and_orders_by_traffic(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    yesterday = NOW - timedelta(days=1)

    for _ in range(3):
        ledger.record(
            source_id="src_busy",
            model_id="model-x",
            usage=ProtocolUsageReport(input_tokens=10),
            at=yesterday,
        )
    ledger.record(
        source_id="src_busy",
        model_id="model-y",
        usage=ProtocolUsageReport(input_tokens=5),
        at=NOW,
    )
    ledger.record(source_id="src_quiet", model_id="model-x", usage=None, at=NOW)

    summary = ledger.summary(days=7, now=NOW)

    assert summary["window_days"] == 7
    assert summary["to_day"] == local_usage_day(NOW).isoformat()
    assert summary["from_day"] == (local_usage_day(NOW) - timedelta(days=6)).isoformat()
    assert [source["source_id"] for source in summary["sources"]] == ["src_busy", "src_quiet"]
    assert [model["model_id"] for model in summary["sources"][0]["models"]] == [
        "model-x",
        "model-y",
    ]
    assert summary["sources"][0]["requests"] == 4
    assert summary["sources"][0]["input_tokens"] == 35
    assert [day["day"] for day in summary["days"]] == sorted(day["day"] for day in summary["days"])
    assert len(summary["days"]) == 2
    assert summary["totals"]["requests"] == 5


def test_summary_reports_a_day_range_even_when_it_carries_no_turn(tmp_path: Path) -> None:
    """Days are the window's frame; only days with data become entries."""

    summary = _ledger(tmp_path).summary(days=30, now=NOW)

    assert summary["days"] == []
    assert summary["sources"] == []
    assert summary["totals"]["requests"] == 0
    assert summary["from_day"] < summary["to_day"]


def test_a_window_excludes_days_outside_it(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    ledger.record(source_id="src_a", model_id="model-x", usage=None, at=NOW - timedelta(days=5))
    ledger.record(source_id="src_a", model_id="model-x", usage=None, at=NOW)

    assert len(ledger.window(days=30, now=NOW)) == 2
    assert len(ledger.window(days=2, now=NOW)) == 1


@pytest.mark.parametrize(
    "requested,expected",
    [
        pytest.param(0, 1, id="zero-clamps-up"),
        pytest.param(-7, 1, id="negative-clamps-up"),
        pytest.param(10_000, USAGE_RETENTION_DAYS, id="clamps-to-retention"),
    ],
)
def test_a_requested_window_is_clamped_to_what_is_retained(
    tmp_path: Path,
    requested: int,
    expected: int,
) -> None:
    assert _ledger(tmp_path).summary(days=requested, now=NOW)["window_days"] == expected


def test_retention_drops_rows_past_the_horizon(tmp_path: Path) -> None:
    clock = _Clock(NOW - timedelta(days=10))
    ledger = _ledger(tmp_path, retention_days=3, now=clock)

    ledger.record(source_id="src_a", model_id="model-x", usage=None, at=clock.moment)
    assert len(ledger.window(days=3, now=NOW - timedelta(days=10))) == 1

    # Writing under a later day is what advances the horizon.
    clock.moment = NOW
    ledger.record(source_id="src_a", model_id="model-x", usage=None, at=NOW)
    persisted = json.loads(ledger.path.read_text(encoding="utf-8"))
    assert [row["day"] for row in persisted] == [local_usage_day(NOW).isoformat()]


def test_the_row_cap_keeps_the_newest_rows(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, max_rows=3)

    for index in range(6):
        ledger.record(
            source_id="src_a",
            model_id=f"model-{index}",
            usage=None,
            at=NOW,
        )

    persisted = json.loads(ledger.path.read_text(encoding="utf-8"))
    assert [row["model_id"] for row in persisted] == ["model-3", "model-4", "model-5"]


def test_the_row_cap_evicts_the_least_recently_metered_row(tmp_path: Path) -> None:
    """Review 4959575659 finding 8: evicting by spelling starves a live model.

    `model-a` sorts first by key and last by recency. Evicting by key would drop
    the row that was just written, so every later call for that model would be
    recreated and immediately evicted again while stale rows survived.
    """

    ledger = _ledger(tmp_path, max_rows=2)
    for index, model_id in enumerate(("model-y", "model-z", "model-a")):
        ledger.record(
            source_id="src_a",
            model_id=model_id,
            usage=None,
            at=NOW + timedelta(minutes=index),
        )

    assert [row["model_id"] for row in ledger.window(days=30, now=NOW)] == [
        "model-a",
        "model-z",
    ]

    ledger.record(
        source_id="src_a",
        model_id="model-a",
        usage=None,
        at=NOW + timedelta(minutes=3),
    )

    rows = {row["model_id"]: row for row in ledger.window(days=30, now=NOW)}
    assert rows["model-a"]["requests"] == 2


def test_a_new_call_is_metered_whatever_recency_the_ledger_already_claims(
    tmp_path: Path,
) -> None:
    """Review 4964314764: a clock that ran ahead must not stop metering.

    Every shape a persisted row can use to outrank the present fills a ledger that
    is already at its cap: a day after today, an instant that has not happened, and
    both together. `_recency` evicts the least recently metered, so each of them
    ranks above the call being recorded right now, and none of them reports
    anything — `window` refuses a day it cannot place. Stating the property rather
    than the three shapes means a fourth spelling of "later than now" is covered
    without editing this test.
    """

    clock = _Clock(NOW)
    ledger = _ledger(tmp_path, max_rows=3, now=clock)
    ahead = NOW + timedelta(days=400)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text(
        json.dumps(
            [
                {
                    "day": local_usage_day(day).isoformat(),
                    "source_id": "src_a",
                    "model_id": f"model-{index}",
                    "requests": 1,
                    "token_reports": 0,
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "last_metered_at": instant.isoformat(),
                }
                for index, (day, instant) in enumerate(
                    ((ahead, ahead), (NOW, ahead), (ahead, NOW))
                )
            ]
        ),
        encoding="utf-8",
    )

    ledger.record(source_id="src_a", model_id="model-new", usage=None, at=NOW)

    rows = {row["model_id"]: row for row in ledger.window(days=30, now=NOW)}
    assert rows["model-new"]["requests"] == 1
    assert all(
        datetime.fromisoformat(row["last_metered_at"]) <= clock.moment for row in rows.values()
    )


def test_a_write_never_rewrites_what_a_later_stamped_write_already_persisted(
    tmp_path: Path,
) -> None:
    """Review 4964520496: one call's stamp cannot bound the whole ledger.

    Metering runs off the event loop, so concurrent calls reach the lock in
    whatever order the executor ran them, not in stamp order. Seed one row of
    every shape a later-stamped write can leave behind — an instant this write
    has not reached, and a day it has not reached — then let the earlier-stamped
    write land on top of them. It contributes its own row and nothing else;
    stating that rather than listing the two shapes covers whatever a later write
    persists next.
    """

    clock = _Clock(NOW)
    ledger = _ledger(tmp_path, now=clock)
    for model_id, stamp in (
        ("model-later-instant", NOW + timedelta(minutes=5)),
        ("model-later-day", NOW + timedelta(days=1)),
    ):
        clock.moment = stamp
        ledger.record(source_id="src_a", model_id=model_id, usage=None, at=stamp)
    persisted = {row["model_id"]: row for row in json.loads(ledger.path.read_text("utf-8"))}

    # Captured before both of them, reaching the lock after both.
    ledger.record(source_id="src_a", model_id="model-earlier", usage=None, at=NOW)

    after = {row["model_id"]: row for row in json.loads(ledger.path.read_text("utf-8"))}
    assert {model_id: after.get(model_id) for model_id in persisted} == persisted
    assert after["model-earlier"]["requests"] == 1


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("", id="empty"),
        pytest.param("not json", id="unparseable"),
        pytest.param('{"rows": []}', id="object-instead-of-list"),
        pytest.param("[1, 2, 3]", id="scalar-rows"),
        pytest.param('[{"source_id": "src_a"}]', id="row-missing-day"),
        pytest.param('[{"day": "not-a-day", "source_id": "s", "model_id": "m"}]', id="row-bad-day"),
    ],
)
def test_an_unreadable_ledger_degrades_to_empty_and_keeps_recording(
    tmp_path: Path,
    content: str,
) -> None:
    """A broken optional-feature file disables the feature; it never fails."""

    ledger = _ledger(tmp_path)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text(content, encoding="utf-8")

    assert ledger.window(days=30, now=NOW) == []

    ledger.record(source_id="src_a", model_id="model-x", usage=None, at=NOW)
    assert ledger.summary(days=30, now=NOW)["totals"]["requests"] == 1


def test_a_row_with_unusable_counters_loads_as_zero(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text(
        json.dumps(
            [
                {
                    "day": local_usage_day(NOW).isoformat(),
                    "source_id": "src_a",
                    "model_id": "model-x",
                    "requests": -4,
                    "token_reports": True,
                    "input_tokens": "many",
                    "output_tokens": USAGE_TOKEN_CEILING * 5,
                }
            ]
        ),
        encoding="utf-8",
    )

    row = ledger.window(days=30, now=NOW)[0]
    assert row["requests"] == 0
    assert row["token_reports"] == 0
    assert row["input_tokens"] == 0
    assert row["cached_input_tokens"] == 0
    assert row["output_tokens"] == USAGE_TOKEN_CEILING


@pytest.mark.parametrize(
    "persisted",
    [
        pytest.param("yesterday", id="not-a-datetime"),
        pytest.param("2026-07-23", id="date-only"),
        pytest.param("", id="empty"),
        pytest.param(1753272000, id="epoch-number"),
    ],
)
def test_an_unparseable_instant_degrades_to_absent(tmp_path: Path, persisted: object) -> None:
    """The read surface promises a date-time, so a corrupt value never travels."""

    ledger = _ledger(tmp_path)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text(
        json.dumps(
            [
                {
                    "day": local_usage_day(NOW).isoformat(),
                    "source_id": "src_a",
                    "model_id": "model-x",
                    "requests": 1,
                    "last_metered_at": persisted,
                }
            ]
        ),
        encoding="utf-8",
    )

    row = ledger.window(days=30, now=NOW)[0]
    assert row["requests"] == 1
    assert row["last_metered_at"] is None


def test_the_later_instant_wins_even_when_its_text_sorts_first(tmp_path: Path) -> None:
    """Text order is not time order once two rows carry different offsets."""

    earlier = datetime(2026, 7, 23, 20, 0, tzinfo=timezone(timedelta(hours=8)))
    later = datetime(2026, 7, 23, 13, 0, tzinfo=timezone.utc)
    assert later.isoformat() < earlier.isoformat() and later > earlier

    ledger = _ledger(tmp_path)
    day = local_usage_day(NOW).isoformat()
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text(
        json.dumps(
            [
                {
                    "day": day,
                    "source_id": "src_a",
                    "model_id": "model-x",
                    "requests": 1,
                    "last_metered_at": earlier.isoformat(),
                },
                {
                    "day": day,
                    "source_id": "src_a",
                    "model_id": "model-x",
                    "requests": 1,
                    "last_metered_at": later.isoformat(),
                },
            ]
        ),
        encoding="utf-8",
    )

    assert ledger.window(days=30, now=NOW)[0]["last_metered_at"] == later.isoformat()


def test_a_persisted_cached_count_above_its_input_is_repaired_on_read(tmp_path: Path) -> None:
    """The subset invariant is a read-surface promise, not only a wire check."""

    ledger = _ledger(tmp_path)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text(
        json.dumps(
            [
                {
                    "day": local_usage_day(NOW).isoformat(),
                    "source_id": "src_a",
                    "model_id": "model-x",
                    "requests": 1,
                    "token_reports": 1,
                    "input_tokens": 120,
                    "cached_input_tokens": 900,
                    "output_tokens": 4,
                }
            ]
        ),
        encoding="utf-8",
    )

    row = ledger.window(days=30, now=NOW)[0]
    assert row["input_tokens"] == 120
    assert row["cached_input_tokens"] == 120


def test_a_persisted_report_count_above_its_requests_is_repaired_on_read(
    tmp_path: Path,
) -> None:
    """Review 4959575659 finding 9: coverage can be partial, never over 100%.

    `requests` is self-measured and `token_reports` counts a subset of it, so a
    corrupt file degrades into a smaller true statement rather than claiming more
    reports than there were calls.
    """

    ledger = _ledger(tmp_path)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text(
        json.dumps(
            [
                {
                    "day": local_usage_day(NOW).isoformat(),
                    "source_id": "src_a",
                    "model_id": "model-x",
                    "requests": 3,
                    "token_reports": 40,
                    "input_tokens": 120,
                    "cached_input_tokens": 8,
                    "output_tokens": 4,
                }
            ]
        ),
        encoding="utf-8",
    )

    assert ledger.summary(days=30, now=NOW)["totals"] == {
        "requests": 3,
        "token_reports": 3,
        "input_tokens": 120,
        "cached_input_tokens": 8,
        "output_tokens": 4,
    }


def test_a_day_in_another_valid_iso_spelling_still_lands_in_its_window(
    tmp_path: Path,
) -> None:
    """Review 4960570946: the window compares days this module spelled.

    `date.fromisoformat` also reads `20260723` and `2026-W30-4`, and the window
    bounds are `YYYY-MM-DD` text. A row kept in the file's own spelling would
    therefore sort outside every window it belongs to and vanish from the tab —
    without even being counted as dropped, since it parsed perfectly well.
    """

    ledger = _ledger(tmp_path)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    day = local_usage_day(NOW)
    ledger.path.write_text(
        json.dumps(
            [
                {
                    "day": day.strftime("%Y%m%d"),
                    "source_id": "src_a",
                    "model_id": "model-x",
                    "requests": 1,
                    "token_reports": 1,
                    "input_tokens": 4,
                    "cached_input_tokens": 0,
                    "output_tokens": 2,
                    "last_metered_at": NOW.isoformat(),
                }
            ]
        ),
        encoding="utf-8",
    )

    rows = ledger.window(days=30, now=NOW)

    assert [row["day"] for row in rows] == [day.isoformat()]
    assert ledger.summary(days=30, now=NOW)["totals"]["input_tokens"] == 4


def test_every_decode_failure_degrades_the_ledger_instead_of_raising(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review 4960570946: degradation is by failure category, not by shape.

    `JSONDecodeError` is only one way a file fails to decode. Invalid UTF-8 and
    an integer past the digit limit both raise a plain `ValueError`, deep nesting
    raises `RecursionError`, and any of them escaping the ledger would fail the
    read route and then stop metering for good — the loud failure this
    degradation exists to prevent.
    """

    payloads = (
        b"\xff\xfe not utf-8",
        ("[" + "9" * 8000 + "]").encode("utf-8"),
        b"[" * 10_000 + b"0" + b"]" * 10_000,
    )
    for payload in payloads:
        ledger = _ledger(tmp_path)
        ledger.path.parent.mkdir(parents=True, exist_ok=True)
        ledger.path.write_bytes(payload)

        with caplog.at_level(logging.WARNING):
            assert ledger.window(days=30, now=NOW) == []

        assert any("unreadable" in record.message for record in caplog.records)
        caplog.clear()
        # Metering keeps working, and the next write replaces the broken file.
        ledger.record(
            source_id="src_a",
            model_id="model-x",
            usage=ProtocolUsageReport.of(
                input_tokens=4,
                cached_input_tokens=0,
                output_tokens=2,
            ),
            at=NOW,
        )
        assert ledger.summary(days=30, now=NOW)["totals"]["requests"] == 1


def test_a_persisted_instant_is_published_in_the_shape_the_schema_promises(
    tmp_path: Path,
) -> None:
    """Reviews 4960016618 and 4960570946: this module decides the spelling.

    `datetime.fromisoformat` accepts far more than RFC 3339 does — naive,
    space-separated, and, after an offset, seconds. Republishing the parsed value
    in the file's own offset only moved the problem, so the file now supplies the
    instant and the module supplies the spelling: UTC, hence `+00:00`, whatever
    was written. A naive value is still read in the local calendar the day
    buckets use.
    """

    ledger = _ledger(tmp_path)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    written = {
        "naive": "2026-07-23 12:00",
        # A valid `fromisoformat` spelling with an offset RFC 3339 cannot express.
        "second_bearing_offset": "2026-07-23T12:00:00+00:00:30",
    }
    ledger.path.write_text(
        json.dumps(
            [
                {
                    "day": local_usage_day(NOW).isoformat(),
                    "source_id": f"src_{key}",
                    "model_id": "model-x",
                    "requests": 1,
                    "token_reports": 1,
                    "input_tokens": 4,
                    "cached_input_tokens": 0,
                    "output_tokens": 2,
                    "last_metered_at": value,
                }
                for key, value in written.items()
            ]
        ),
        encoding="utf-8",
    )

    published = {
        row["source_id"]: row["last_metered_at"] for row in ledger.window(days=30, now=NOW)
    }

    assert published["src_naive"] == (
        datetime(2026, 7, 23, 12, 0).astimezone(timezone.utc).isoformat()
    )
    assert published["src_second_bearing_offset"] == "2026-07-23T11:59:30+00:00"
    assert all(value.endswith("+00:00") for value in published.values())


def test_a_written_row_is_validated_the_same_way_a_persisted_one_is(
    tmp_path: Path,
) -> None:
    """One validation path: what this module writes, it could also have read."""

    ledger = _ledger(tmp_path)
    ledger.record(
        source_id="src_a",
        model_id="model-x",
        usage=ProtocolUsageReport(input_tokens=40, cached_input_tokens=12, output_tokens=7),
        at=NOW,
    )

    persisted = json.loads(ledger.path.read_text(encoding="utf-8"))
    assert persisted == ledger.window(days=30, now=NOW)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("not json", id="unparseable"),
        pytest.param('{"rows": []}', id="object-instead-of-list"),
        pytest.param('[{"day": "not-a-day", "source_id": "s", "model_id": "m"}]', id="bad-row"),
    ],
)
def test_rejecting_existing_ledger_state_is_warned_not_silent(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    content: str,
) -> None:
    """Review 4959575659 finding 12: the next write erases what was rejected.

    Degrading to empty is the persisted-shape rule; doing it quietly would spend
    the last moment the old history was recoverable.
    """

    ledger = _ledger(tmp_path)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text(content, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="core.handlers.model_hub.usage"):
        assert ledger.window(days=30, now=NOW) == []

    assert [record.levelno for record in caplog.records] == [logging.WARNING]


def test_duplicate_persisted_rows_merge_instead_of_shadowing(tmp_path: Path) -> None:
    """An older file could hold the same key twice; both counts must survive."""

    ledger = _ledger(tmp_path)
    row = {
        "day": local_usage_day(NOW).isoformat(),
        "source_id": "src_a",
        "model_id": "model-x",
        "requests": 2,
        "token_reports": 2,
        "input_tokens": 50,
        "cached_input_tokens": 0,
        "output_tokens": 5,
    }
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text(json.dumps([row, dict(row)]), encoding="utf-8")

    rows = ledger.window(days=30, now=NOW)
    assert len(rows) == 1
    assert rows[0]["requests"] == 4
    assert rows[0]["input_tokens"] == 100


def test_accumulated_counters_stop_at_the_ceiling(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    saturated = ProtocolUsageReport(input_tokens=USAGE_TOKEN_CEILING)

    ledger.record(source_id="src_a", model_id="model-x", usage=saturated, at=NOW)
    ledger.record(source_id="src_a", model_id="model-x", usage=saturated, at=NOW)

    assert ledger.summary(days=30, now=NOW)["totals"]["input_tokens"] == USAGE_TOKEN_CEILING


@pytest.mark.parametrize(
    "source_id,model_id",
    [
        pytest.param("", "model-x", id="empty-source"),
        pytest.param("   ", "model-x", id="blank-source"),
        pytest.param("src_a", "m" * (MODEL_ID_MAX_LENGTH + 1), id="oversized-model"),
    ],
)
def test_an_unusable_identifier_is_never_persisted(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    source_id: str,
    model_id: str,
) -> None:
    """Loud, not silent: a dropped turn is lost metering, not a tidy no-op."""

    ledger = _ledger(tmp_path)

    with caplog.at_level(logging.WARNING, logger="core.handlers.model_hub.usage"):
        ledger.record(source_id=source_id, model_id=model_id, usage=None, at=NOW)

    assert ledger.window(days=30, now=NOW) == []
    assert [record.levelno for record in caplog.records] == [logging.WARNING]


def test_the_longest_admissible_model_id_is_still_metered(tmp_path: Path) -> None:
    """One constant bounds admission and metering, so neither can drop the other's."""

    ledger = _ledger(tmp_path)
    longest = "m" * MODEL_ID_MAX_LENGTH

    ledger.record(source_id="src_a", model_id=longest, usage=None, at=NOW)

    assert [row["model_id"] for row in ledger.window(days=30, now=NOW)] == [longest]


def test_the_ledger_file_is_owner_only(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    ledger.record(source_id="src_a", model_id="model-x", usage=None, at=NOW)

    assert stat.S_IMODE(os.stat(ledger.path).st_mode) == 0o600


def test_a_record_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    ledger.record(source_id="src_a", model_id="model-x", usage=None, at=NOW)

    assert [entry.name for entry in ledger.path.parent.iterdir()] == [ledger.path.name]


def test_a_write_that_fails_leaves_neither_residue_nor_a_damaged_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MH-USAGE-003, review 4965677908: a failed replacement must cost only that write.

    The recorder above this ledger swallows `OSError` so a full disk cannot break
    metering, which means nobody downstream ever sees what a failed write left
    behind. A ledger bounded to `max_rows` whose state directory gains one orphan
    per failure is not bounded, and a half-written document would lose the days
    already recorded — so the write either lands whole or changes nothing.
    """

    ledger = _ledger(tmp_path)
    ledger.record(source_id="src_a", model_id="model-x", usage=None, at=NOW)
    recorded = ledger.path.read_bytes()

    def refuse(*_args, **_kwargs) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(state_file.os, "replace", refuse)
    with pytest.raises(OSError):
        ledger.record(source_id="src_b", model_id="model-y", usage=None, at=NOW)

    assert [entry.name for entry in ledger.path.parent.iterdir()] == [ledger.path.name]
    assert ledger.path.read_bytes() == recorded


def test_days_bucket_by_the_host_calendar_not_utc(tmp_path: Path) -> None:
    """Avibe is local-first, so a day boundary is the user's midnight."""

    moment = datetime(2026, 7, 23, 21, 30, tzinfo=timezone.utc)
    expected = datetime.fromtimestamp(moment.timestamp()).date()

    assert local_usage_day(moment) == expected

    ledger = _ledger(tmp_path)
    ledger.record(source_id="src_a", model_id="model-x", usage=None, at=moment)
    assert ledger.window(days=1, now=moment)[0]["day"] == expected.isoformat()


def test_one_local_day_holds_moments_from_two_utc_days(tmp_path: Path) -> None:
    """Bucketing must follow the host's calendar consistently, whatever it is."""

    ledger = _ledger(tmp_path)
    local_noon = datetime.combine(date(2026, 7, 23), datetime.min.time()).astimezone() + timedelta(hours=12)

    for offset in (timedelta(hours=-11), timedelta(hours=11)):
        ledger.record(source_id="src_a", model_id="model-x", usage=None, at=local_noon + offset)

    rows = ledger.window(days=1, now=local_noon)
    assert len(rows) == 1
    assert rows[0]["day"] == "2026-07-23"
    assert rows[0]["requests"] == 2
