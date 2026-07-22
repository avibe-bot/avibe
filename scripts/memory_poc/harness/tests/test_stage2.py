from __future__ import annotations

import memory_poc.stage2 as stage2

from memory_poc.constants import CRITERIA_IDS
from memory_poc.errors import HarnessError
from memory_poc.provider import HttpShape
from memory_poc.stage2 import (
    _error_shape_lines,
    _quality_evidence_lines,
    _set_aggregate_boolean,
    _wait_same_timestamp_distinct,
    percentile,
    recommendation_for_criteria,
    recommendation_for_report,
)


def _criteria(state: str = "pass") -> list[dict[str, object]]:
    return [
        {
            "id": criterion_id,
            "state": state,
            "value": 1 if state != "not_measured" else None,
            "threshold": 1 if state != "not_measured" else None,
        }
        for criterion_id in CRITERIA_IDS
    ]


def test_recommendation_requires_every_criterion_before_official() -> None:
    criteria = _criteria("pass")
    assert recommendation_for_criteria(criteria) == "official"

    criteria[0] = {"id": criteria[0]["id"], "state": "not_measured", "value": None, "threshold": None}
    assert recommendation_for_criteria(criteria) == "fork"


def test_primary_quality_failure_stops_the_provider_decision() -> None:
    criteria = _criteria("pass")
    criteria[0] = {"id": "temporal_all", "state": "fail", "value": 0, "threshold": 1}

    assert recommendation_for_criteria(criteria) == "stop"


def test_ambiguous_duplicate_outcome_cannot_be_called_official() -> None:
    report = {"criteria": _criteria("pass"), "duplicates": {"observed": "same timestamp distinct false", "count": 0}}

    assert recommendation_for_report(report) == "fork"


def test_incomplete_timed_retry_or_duplicate_total_blocks_official() -> None:
    criteria = _criteria("pass")

    timed_retry = {"criteria": criteria, "duplicates": {"observed": "timed retry exercised false", "count": 0}}
    unknown_total = {
        "criteria": _criteria("pass"),
        "duplicates": {"observed": "total logical count not measured", "count": 0},
    }

    assert recommendation_for_report(timed_retry) == "fork"
    assert recommendation_for_report(unknown_total) == "fork"


def test_percentile_uses_nearest_rank_and_preserves_timeout_values() -> None:
    assert percentile((1, 2, 3, 4, 5), 0.95) == 5
    assert percentile((100, 300001), 0.95) == 300001


def test_error_shape_evidence_is_value_free_and_summary_safe() -> None:
    lines = _error_shape_lines(
        "invalid credential",
        (
            HttpShape(
                phase="ingestion",
                method="POST",
                route="/api/v1/memory/add",
                status_code=401,
                request_keys=("messages",),
                response_keys=("error",),
                data_keys=(),
                response_schema_paths=("error.code:string", "error.details[].field:string"),
                closed_code=None,
            ),
        ),
    )

    assert lines == (
        "error invalid credential public shape route /api/v1/memory/add status 401 closed absent "
        "request keys messages response keys error data keys none paths error.code=string,error.details_array.field=string",
    )


def test_quality_evidence_records_expected_identity_and_public_result_identity() -> None:
    from memory_poc.corpus import QueryEvaluation, SearchItem, load_corpus

    query = load_corpus().query("q004")
    lines = _quality_evidence_lines(
        "q1-q004",
        query,
        QueryEvaluation(passed=True, expected_rank=1, forbidden_rank=None),
        (SearchItem(kind="episode", text="not persisted", rank=1, identity="episode-123"),),
    )

    assert lines == (
        "quality q1-q004 expected episode s1 seq 7 pass true rank 1",
        "quality q1-q004 returned 1-1 episode_rank1_idepisode-123",
    )


def test_shared_boolean_criteria_preserve_an_earlier_failure() -> None:
    report = {"criteria": _criteria("not_measured")}

    _set_aggregate_boolean(report, "restart_preserves", False)
    _set_aggregate_boolean(report, "restart_preserves", True)

    criterion = next(item for item in report["criteria"] if item["id"] == "restart_preserves")
    assert criterion == {"id": "restart_preserves", "state": "fail", "value": 0, "threshold": 1}


def test_same_timestamp_probe_requires_distinct_public_identities() -> None:
    class Client:
        def search(self, *, query: str, **_kwargs: object) -> dict[str, object]:
            if "alpha" in query:
                return {"episodes": [{"user_id": "00000000-0000-4000-8000-000000000002", "id": "a", "summary": "alpha"}]}
            return {"episodes": [{"user_id": "00000000-0000-4000-8000-000000000002", "id": "b", "summary": "bravo"}]}

    observation = _wait_same_timestamp_distinct(Client())  # type: ignore[arg-type]

    assert observation.alpha_identities == 1
    assert observation.bravo_identities == 1
    assert observation.distinct is True


def test_wait_searchable_or_none_forwards_the_quality_observation_timeout(monkeypatch: object) -> None:
    observed: dict[str, float] = {}

    def timeout(*_args: object, timeout_seconds: float, **_kwargs: object) -> int:
        observed["timeout_seconds"] = timeout_seconds
        raise HarnessError("searchable_timeout")

    monkeypatch.setattr(stage2, "_wait_searchable", timeout)  # type: ignore[attr-defined]

    result = stage2._wait_searchable_or_none(  # type: ignore[attr-defined]
        object(),
        owner_id="owner",
        query="query",
        hints=("hint",),
        timeout_seconds=15.0,
    )

    assert result is None
    assert observed == {"timeout_seconds": 15.0}
