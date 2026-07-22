from __future__ import annotations

import json
from pathlib import Path

import pytest

import memory_poc.sanity as sanity
from memory_poc.environment import ProviderSettings
from memory_poc.errors import HarnessError
from memory_poc.metrics import CallMetrics
from memory_poc.sanity import SanityFixture, _contains_atomic_fact, _contains_owned_items, _failure_outcome

OWNER_ID = "00000000-0000-4000-8000-000000000001"


def test_sanity_checks_the_pinned_hybrid_atomic_fact_shape() -> None:
    profile = {"profiles": [{"id": "profile-1", "user_id": OWNER_ID, "profile_data": {"language": "Python"}}]}
    episodes = {"episodes": [{"id": "episode-1", "user_id": OWNER_ID}]}
    search = {
        "episodes": [
            {
                "id": "episode-1",
                "user_id": OWNER_ID,
                "atomic_facts": [{"id": "fact-1", "content": "The owner uses Python.", "score": 0.9}],
            }
        ]
    }

    assert _contains_owned_items(profile, key="profiles", owner_id=OWNER_ID)
    assert _contains_owned_items(episodes, key="episodes", owner_id=OWNER_ID)
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
    def __init__(self, *, profile: dict[str, object], episodes: dict[str, object], facts: dict[str, object]) -> None:
        self.profile = profile
        self.episodes = episodes
        self.facts = facts
        self.search_calls = 0

    def get(self, *, memory_type: str, **_kwargs: object) -> dict[str, object]:
        return self.profile if memory_type == "profile" else self.episodes

    def search(self, **_kwargs: object) -> dict[str, object]:
        self.search_calls += 1
        return self.facts


def _fixture() -> SanityFixture:
    return SanityFixture(
        session_id="stage1-session",
        owner_id=OWNER_ID,
        query="Python",
        fact_hint="Python",
        messages=[],
    )


def _install_test_clock(monkeypatch: pytest.MonkeyPatch, *, timeout_seconds: float) -> None:
    clock = [0.0]
    monkeypatch.setattr(sanity, "_READINESS_TIMEOUT_SECONDS", timeout_seconds)
    monkeypatch.setattr(sanity.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(sanity.time, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration))


def test_sanity_waits_for_profile_and_episodes_before_search(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_test_clock(monkeypatch, timeout_seconds=1.0)
    client = _ReadinessClient(profile={"profiles": []}, episodes={"episodes": []}, facts={"episodes": []})

    with pytest.raises(HarnessError, match="sanity_memory_not_ready"):
        sanity._read_required_memory(client, _fixture())

    assert client.search_calls == 0


def test_sanity_bounds_searches_while_facts_are_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_test_clock(monkeypatch, timeout_seconds=1.0)
    client = _ReadinessClient(
        profile={"profiles": [{"user_id": OWNER_ID}]},
        episodes={"episodes": [{"user_id": OWNER_ID}]},
        facts={"episodes": []},
    )

    with pytest.raises(HarnessError, match="sanity_memory_not_ready"):
        sanity._read_required_memory(client, _fixture())

    assert client.search_calls == 1


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
        lambda *_args, **_kwargs: (_ for _ in ()).throw(HarnessError("sanity_memory_not_ready")),
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
    assert "synthetic fixture body" not in summary
