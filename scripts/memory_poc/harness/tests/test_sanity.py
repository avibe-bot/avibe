from __future__ import annotations

import json
from pathlib import Path

import pytest

import memory_poc.sanity as sanity
from memory_poc.environment import ProviderSettings
from memory_poc.errors import HarnessError
from memory_poc.metrics import CallMetrics
from memory_poc.readiness import SearchReadiness
from memory_poc.sanity import (
    SanityFixture,
    _contains_atomic_fact,
    _contains_search_episode,
    _contains_search_profile,
    _failure_outcome,
)

OWNER_ID = "00000000-0000-4000-8000-000000000001"


def test_sanity_checks_the_pinned_hybrid_atomic_fact_shape() -> None:
    search = {
        "profiles": [{"id": "profile-1", "user_id": OWNER_ID, "profile_data": {"language": "Python"}}],
        "episodes": [
            {
                "id": "episode-1",
                "user_id": OWNER_ID,
                "summary": "The owner uses Python.",
                "atomic_facts": [{"id": "fact-1", "content": "The owner uses Python.", "score": 0.9}],
            }
        ]
    }

    assert _contains_search_profile(search, owner_id=OWNER_ID, content_hint="Python")
    assert _contains_search_episode(search, owner_id=OWNER_ID, content_hint="Python")
    assert _contains_atomic_fact(search, owner_id=OWNER_ID, fact_hint="Python")


def test_sanity_does_not_accept_legacy_kind_markers_or_another_owner() -> None:
    unsupported_shape = {"items": [{"kind": "atomic_fact", "content": "Python"}]}
    wrong_owner = {
        "episodes": [
            {
                "user_id": "different-owner",
                "atomic_facts": [{"id": "fact-1", "content": "Python", "score": 0.9}],
            }
        ]
    }

    assert not _contains_atomic_fact(unsupported_shape, owner_id=OWNER_ID, fact_hint="Python")
    assert not _contains_atomic_fact(wrong_owner, owner_id=OWNER_ID, fact_hint="Python")


class _ReadinessClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.search_calls = 0

    def get(self, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("readiness_must_not_use_get")

    def search(self, **_kwargs: object) -> dict[str, object]:
        index = min(self.search_calls, len(self.responses) - 1)
        self.search_calls += 1
        return self.responses[index]


def _fixture() -> SanityFixture:
    return SanityFixture(
        session_id="stage1-session",
        owner_id=OWNER_ID,
        query="Which language does the synthetic owner use?",
        fact_hint="Python",
        messages=[],
    )


def _install_test_clock(monkeypatch: pytest.MonkeyPatch, *, timeout_seconds: float) -> None:
    clock = [0.0]
    monkeypatch.setattr(sanity, "_READINESS_TIMEOUT_SECONDS", timeout_seconds)
    monkeypatch.setattr(sanity.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(sanity.time, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration))


def _search_response(*, profile: bool = False, episode: bool = False, fact: bool = False) -> dict[str, object]:
    profiles: list[dict[str, object]] = []
    episodes: list[dict[str, object]] = []
    if profile:
        profiles.append({"user_id": OWNER_ID, "profile_data": {"language": "Python"}})
    if episode or fact:
        entry: dict[str, object] = {"user_id": OWNER_ID}
        if episode:
            entry["summary"] = "The owner uses Python."
        if fact:
            entry["atomic_facts"] = [{"id": "fact-1", "content": "The owner uses Python."}]
        episodes.append(entry)
    return {"profiles": profiles, "episodes": episodes}


def test_sanity_readiness_uses_search_without_get(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_test_clock(monkeypatch, timeout_seconds=1.0)
    client = _ReadinessClient([_search_response()])

    readiness = sanity._read_required_memory(client, _fixture())

    assert readiness == SearchReadiness(profile_ms=None, episode_ms=None, atomic_fact_ms=None, timeout_ms=1000)
    assert client.search_calls == 1


def test_sanity_records_first_search_appearance_times(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_test_clock(monkeypatch, timeout_seconds=30.0)
    client = _ReadinessClient(
        [
            _search_response(),
            _search_response(profile=True),
            _search_response(profile=True, episode=True),
            _search_response(profile=True, episode=True, fact=True),
        ],
    )

    readiness = sanity._read_required_memory(client, _fixture())

    assert readiness == SearchReadiness(profile_ms=5000, episode_ms=10000, atomic_fact_ms=15000, timeout_ms=30000)
    assert readiness.max_retrieval_ms == 15000
    assert readiness.retrieval_complete
    assert readiness.profile_retrieved
    assert client.search_calls == 4


def test_sanity_keeps_profile_observation_open_after_retrieval_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_test_clock(monkeypatch, timeout_seconds=20.0)
    client = _ReadinessClient([_search_response(episode=True, fact=True)])

    readiness = sanity._read_required_memory(client, _fixture())

    assert readiness == SearchReadiness(profile_ms=None, episode_ms=0, atomic_fact_ms=0, timeout_ms=20000)
    assert readiness.retrieval_complete
    assert not readiness.profile_retrieved
    assert client.search_calls == 4


def test_sanity_restart_readiness_skips_the_profile_warning_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_test_clock(monkeypatch, timeout_seconds=30.0)
    client = _ReadinessClient([_search_response(episode=True, fact=True)])

    readiness = sanity._read_required_memory(client, _fixture(), wait_for_profile=False)

    assert readiness.retrieval_complete
    assert client.search_calls == 1


def test_sanity_does_not_count_search_results_after_the_readiness_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(sanity, "_READINESS_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(sanity.time, "monotonic", lambda: clock[0])

    class SlowClient:
        def search(self, **_kwargs: object) -> dict[str, object]:
            clock[0] += 2.0
            return _search_response(profile=True, episode=True, fact=True)

    readiness = sanity._read_required_memory(SlowClient(), _fixture())

    assert readiness == SearchReadiness(profile_ms=None, episode_ms=None, atomic_fact_ms=None, timeout_ms=1000)


def test_sanity_failure_outcome_never_carries_unstructured_error_text() -> None:
    assert _failure_outcome(HarnessError("sanity_memory_not_ready")) == "sanity_memory_not_ready"
    assert _failure_outcome(HarnessError("provider said: sensitive detail")) == "harness_failure"


def test_sanity_failure_writes_redacted_partial_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = ProviderSettings(
        llm_base_url="https://example.invalid/v1",
        llm_model="llm-model",
        llm_api_key="not-a-real-key",
        embedding_base_url="https://example.invalid/v1",
        embedding_model="embedding-model",
        embedding_api_key="not-a-real-key",
        source=tmp_path / ".env.poc",
    )
    fixture = SanityFixture(
        session_id="stage1-session",
        owner_id=OWNER_ID,
        query="Python",
        fact_hint="Python",
        messages=[{"content": "synthetic fixture body"}],
    )

    class Client:
        observed_http_shapes: tuple[object, ...] = ()

        def add(self, **_kwargs: object) -> dict[str, object]:
            return {}

        def flush(self, **_kwargs: object) -> dict[str, object]:
            return {}

    class Process:
        def __init__(self, **_kwargs: object) -> None:
            self.client = Client()

        def start(self) -> Client:
            return self.client

        def stop(self) -> None:
            return None

    monkeypatch.setattr(sanity, "checked_workspace_root", lambda _workspace: workspace)
    monkeypatch.setattr(sanity, "discover_provider_settings", lambda _workspace: settings)
    monkeypatch.setattr(sanity, "assert_clean_harness_source", lambda _workspace: None)
    monkeypatch.setattr(sanity, "locked_environment_python", lambda _workspace: tmp_path / "python")
    monkeypatch.setattr(sanity, "verify_locked_environment", lambda python: python)
    monkeypatch.setattr(sanity, "load_sanity_fixture", lambda: fixture)
    monkeypatch.setattr(sanity, "write_generated_config", lambda **_kwargs: None)
    monkeypatch.setattr(sanity, "EverOSProcess", Process)
    monkeypatch.setattr(
        sanity,
        "_read_required_memory",
        lambda *_args, **_kwargs: SearchReadiness(
            profile_ms=5000,
            episode_ms=None,
            atomic_fact_ms=None,
            timeout_ms=600000,
        ),
    )
    monkeypatch.setattr(sanity, "read_call_metrics", lambda _path: CallMetrics(llm_calls=2, embedding_calls=3))

    with pytest.raises(HarnessError, match="sanity_memory_not_ready"):
        sanity.run_sanity(run_id="r1", workspace=workspace)

    run_dir = workspace / ".runtime" / "memory-poc" / "runs" / "r1"
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    summary = (run_dir / "summary.md").read_text(encoding="utf-8")
    criteria = {item["id"]: item for item in report["criteria"]}
    assert criteria["launcher_uds_only"] == {
        "id": "launcher_uds_only",
        "state": "pass",
        "value": 1,
        "threshold": 1,
    }
    assert criteria["restart_preserves"] == {
        "id": "restart_preserves",
        "state": "not_measured",
        "value": None,
        "threshold": None,
    }
    assert report["resources"]["llm_calls"] == 2
    assert report["resources"]["embedding_calls"] == 3
    assert "Run outcome: sanity_memory_not_ready" in summary
    assert "Profile content via search: first observed 5000 ms after flush completion." in summary
    assert "Episode content via search: not observed within 600000 ms after flush completion." in summary
    assert "synthetic fixture body" not in summary


def test_sanity_passes_core_retrieval_with_a_profile_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = ProviderSettings(
        llm_base_url="https://example.invalid/v1",
        llm_model="llm-model",
        llm_api_key="not-a-real-key",
        embedding_base_url="https://example.invalid/v1",
        embedding_model="embedding-model",
        embedding_api_key="not-a-real-key",
        source=tmp_path / ".env.poc",
    )
    fixture = SanityFixture(
        session_id="stage1-session",
        owner_id=OWNER_ID,
        query="Which language does the synthetic owner use?",
        fact_hint="Python",
        messages=[{"content": "synthetic fixture body"}],
    )

    class Client:
        observed_http_shapes: tuple[object, ...] = ()

        def add(self, **_kwargs: object) -> dict[str, object]:
            return {}

        def flush(self, **_kwargs: object) -> dict[str, object]:
            return {}

    class Process:
        def __init__(self, **_kwargs: object) -> None:
            self.client = Client()

        def start(self) -> Client:
            return self.client

        def stop(self) -> None:
            return None

    readiness = iter(
        (
            SearchReadiness(profile_ms=None, episode_ms=6103, atomic_fact_ms=49143, timeout_ms=600000),
            SearchReadiness(profile_ms=None, episode_ms=500, atomic_fact_ms=500, timeout_ms=600000),
        )
    )
    monkeypatch.setattr(sanity, "checked_workspace_root", lambda _workspace: workspace)
    monkeypatch.setattr(sanity, "discover_provider_settings", lambda _workspace: settings)
    monkeypatch.setattr(sanity, "assert_clean_harness_source", lambda _workspace: None)
    monkeypatch.setattr(sanity, "locked_environment_python", lambda _workspace: tmp_path / "python")
    monkeypatch.setattr(sanity, "verify_locked_environment", lambda python: python)
    monkeypatch.setattr(sanity, "load_sanity_fixture", lambda: fixture)
    monkeypatch.setattr(sanity, "write_generated_config", lambda **_kwargs: None)
    monkeypatch.setattr(sanity, "EverOSProcess", Process)
    monkeypatch.setattr(sanity, "_read_required_memory", lambda *_args, **_kwargs: next(readiness))
    monkeypatch.setattr(sanity, "_storage_exists", lambda _root: True)
    monkeypatch.setattr(sanity, "read_call_metrics", lambda _path: CallMetrics(llm_calls=2, embedding_calls=3))

    report_path = sanity.run_sanity(run_id="r2", workspace=workspace)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    summary = (report_path.parent / "summary.md").read_text(encoding="utf-8")
    criteria = {item["id"]: item for item in report["criteria"]}
    assert criteria["searchable_p95_min"] == {
        "id": "searchable_p95_min",
        "state": "pass",
        "value": pytest.approx(49143 / 60000),
        "threshold": 5.0,
    }
    assert criteria["restart_preserves"]["state"] == "pass"
    assert criteria["no_internals_needed"]["state"] == "pass"
    assert "Run outcome: pass_with_profile_warning" in summary
    assert "profile content not retrievable via /search within the window; episode+fact retrieval succeeded; profile treated as known-absent" in summary
    assert "profile not published/readable via public search in 1.1.3 + qwen3.7; accepted as known behavior for MVP." in summary
    assert "sanity_memory_not_ready" not in summary
