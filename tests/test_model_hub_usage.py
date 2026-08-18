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


def _ledger(tmp_path: Path, **kwargs) -> BoundedUsageLedger:
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
    ledger = _ledger(tmp_path, retention_days=3)

    ledger.record(source_id="src_a", model_id="model-x", usage=None, at=NOW - timedelta(days=10))
    assert len(ledger.window(days=3, now=NOW - timedelta(days=10))) == 1

    # Recording under a later day is what advances the horizon.
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
