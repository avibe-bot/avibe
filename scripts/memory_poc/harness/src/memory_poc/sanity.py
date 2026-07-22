from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .environment import (
    assert_clean_harness_source,
    checked_workspace_root,
    discover_provider_settings,
    locked_environment_python,
    verify_locked_environment,
)
from .errors import HarnessError, LaunchError
from .generated_config import write_generated_config
from .identifiers import validate_run_id
from .launcher import EverOSProcess
from .metrics import read_call_metrics
from .paths import ensure_owner_directory, runtime_root, write_private_text
from .readiness import SearchReadiness
from .reports import build_report, set_criterion, write_report, write_summary
from .reports import local_timezone_name

_READINESS_TIMEOUT_SECONDS = 600.0
_SEARCH_POLL_SECONDS = 5.0


@dataclass(frozen=True)
class SanityFixture:
    session_id: str
    owner_id: str
    query: str
    fact_hint: str
    messages: list[dict[str, Any]]


def run_sanity(*, run_id: str, workspace: Path | None = None) -> Path:
    validate_run_id(run_id)
    root = checked_workspace_root(workspace)
    fixture = load_sanity_fixture()
    settings = discover_provider_settings(root)
    assert_clean_harness_source(root)
    python = verify_locked_environment(locked_environment_python(root))
    state = ensure_owner_directory(runtime_root(root), anchor=root)
    run_dir = state / "runs" / run_id
    if run_dir.exists():
        raise HarnessError("run_id_already_exists")
    ensure_owner_directory(run_dir, anchor=state)
    logs_dir = ensure_owner_directory(run_dir / "logs", anchor=state)
    everos_root = ensure_owner_directory(run_dir / "everos-root", anchor=state)
    child_home = ensure_owner_directory(run_dir / "child-home", anchor=state)
    write_generated_config(everos_root=everos_root, timezone=local_timezone_name(), anchor=state)
    metrics_path = logs_dir / "request-counts.jsonl"
    fixture_texts = tuple(message["content"] for message in fixture.messages)
    write_private_text(
        run_dir / "run.json",
        json.dumps({"stage": "sanity", "fixture_set": "stage1-mini"}, ensure_ascii=True) + "\n",
        anchor=state,
    )

    first = EverOSProcess(
        python=python,
        everos_root=everos_root,
        child_home=child_home,
        state_root=state,
        settings=settings,
        metrics_path=metrics_path,
        owner_id=fixture.owner_id,
    )
    first_shapes = ()
    restarted_shapes = ()
    first_started = False
    restart_preserved = False
    add_ms: int | None = None
    flush_ms: int | None = None
    first_searchable_ms: int | None = None
    readiness = SearchReadiness.not_measured(timeout_ms=_readiness_timeout_ms())
    failure: HarnessError | None = None
    try:
        client = None
        try:
            client = first.start()
            first_started = True
            add_ms = _elapsed_ms(lambda: client.add(session_id=fixture.session_id, messages=fixture.messages))
            flush_started = time.monotonic()
            client.flush(session_id=fixture.session_id)
            flush_completed_at = time.monotonic()
            flush_ms = int((flush_completed_at - flush_started) * 1000)
            readiness = _read_required_memory(client, fixture, started_at=flush_completed_at)
            if not readiness.complete:
                raise HarnessError("sanity_memory_not_ready")
            first_searchable_ms = readiness.max_observed_ms
        finally:
            if client is not None:
                first_shapes = client.observed_http_shapes
            first.stop()

        second = EverOSProcess(
            python=python,
            everos_root=everos_root,
            child_home=child_home,
            state_root=state,
            settings=settings,
            metrics_path=metrics_path,
            owner_id=fixture.owner_id,
        )
        restarted_client = None
        try:
            restarted_client = second.start()
            restart_readiness = _read_required_memory(restarted_client, fixture)
            if not restart_readiness.complete:
                raise HarnessError("sanity_memory_not_ready")
            restart_preserved = True
        finally:
            if restarted_client is not None:
                restarted_shapes = restarted_client.observed_http_shapes
            second.stop()

        if not _storage_exists(everos_root):
            raise HarnessError("sanity_storage_layout_missing")
    except HarnessError as exc:
        failure = exc

    metrics = read_call_metrics(metrics_path)
    report = build_report(run_id=run_id, settings=settings)
    if first_started:
        set_criterion(report["criteria"], "launcher_uds_only", state="pass", value=1, threshold=1)
    if restart_preserved:
        set_criterion(report["criteria"], "restart_preserves", state="pass", value=1, threshold=1)
        set_criterion(report["criteria"], "no_internals_needed", state="pass", value=1, threshold=1)
    report["latency"] = {
        "add_ms": {"sanity": add_ms} if add_ms is not None else {},
        "flush_ms": {"sanity": flush_ms} if flush_ms is not None else {},
        "searchable_ms": {"sanity": first_searchable_ms} if first_searchable_ms is not None else {},
        "query_ms": {},
    }
    report["resources"]["llm_calls"] = metrics.llm_calls
    report["resources"]["embedding_calls"] = metrics.embedding_calls
    report["duplicates"] = {"observed": "not_run", "count": 0}
    report_path = run_dir / "report.json"
    write_report(report_path, report, anchor=state, fixture_texts=fixture_texts)
    write_summary(
        run_dir / "summary.md",
        settings=settings,
        metrics=metrics,
        message_count=len(fixture.messages),
        http_shapes=first_shapes + restarted_shapes,
        outcome="completed" if failure is None else _failure_outcome(failure),
        readiness=readiness,
        anchor=state,
    )
    if failure is not None:
        raise failure
    return report_path


def load_sanity_fixture() -> SanityFixture:
    fixture_path = Path(__file__).resolve().parents[2] / "testdata" / "sanity_messages.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        raise HarnessError("sanity_fixture_invalid")
    now_ms = time.time_ns() // 1_000_000
    messages: list[dict[str, Any]] = []
    for item in payload["messages"]:
        if not isinstance(item, dict):
            raise HarnessError("sanity_fixture_invalid")
        messages.append(
            {
                "sender_id": str(item["sender_id"]),
                "role": str(item["role"]),
                "timestamp": now_ms + int(item["offset_ms"]),
                "content": str(item["content"]),
            }
        )
    return SanityFixture(
        session_id=str(payload["session_id"]),
        owner_id=str(payload["owner_id"]),
        query=str(payload["query"]),
        fact_hint=str(payload["fact_hint"]),
        messages=messages,
    )


def _elapsed_ms(callback: Any) -> int:
    started = time.monotonic()
    callback()
    return int((time.monotonic() - started) * 1000)


def _failure_outcome(error: HarnessError) -> str:
    code = str(error)
    if (
        0 < len(code) <= 128
        and code.isascii()
        and code[0].isalnum()
        and all(character.isalnum() or character in "_.-" for character in code)
    ):
        return code
    return "harness_failure"


def _read_required_memory(client: Any, fixture: SanityFixture, *, started_at: float | None = None) -> SearchReadiness:
    measurement_start = time.monotonic() if started_at is None else started_at
    deadline = measurement_start + _READINESS_TIMEOUT_SECONDS
    readiness = SearchReadiness.pending(timeout_ms=_readiness_timeout_ms())
    while time.monotonic() < deadline:
        try:
            result = client.search(owner_id=fixture.owner_id, query=fixture.query)
        except LaunchError:
            _sleep_until_next_search(deadline)
            continue
        now = time.monotonic()
        if now > deadline:
            return readiness
        elapsed_ms = int((now - measurement_start) * 1000)
        if readiness.profile_ms is None and _contains_search_profile(
            result,
            owner_id=fixture.owner_id,
            content_hint=fixture.fact_hint,
        ):
            readiness = replace(readiness, profile_ms=elapsed_ms)
        if readiness.episode_ms is None and _contains_search_episode(
            result,
            owner_id=fixture.owner_id,
            content_hint=fixture.fact_hint,
        ):
            readiness = replace(readiness, episode_ms=elapsed_ms)
        if readiness.atomic_fact_ms is None and _contains_atomic_fact(
            result,
            owner_id=fixture.owner_id,
            fact_hint=fixture.fact_hint,
        ):
            readiness = replace(readiness, atomic_fact_ms=elapsed_ms)
        if readiness.complete:
            return readiness
        _sleep_until_next_search(deadline)
    return readiness


def _sleep_until_next_search(deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(min(_SEARCH_POLL_SECONDS, remaining))


def _readiness_timeout_ms() -> int:
    return int(_READINESS_TIMEOUT_SECONDS * 1000)


def _contains_search_profile(value: Any, *, owner_id: str, content_hint: str) -> bool:
    if not isinstance(value, dict):
        return False
    profiles = value.get("profiles")
    if not isinstance(profiles, list):
        return False
    return any(
        isinstance(profile, dict)
        and profile.get("user_id") == owner_id
        and _contains_text(profile.get("profile_data"), content_hint)
        for profile in profiles
    )


def _contains_search_episode(value: Any, *, owner_id: str, content_hint: str) -> bool:
    for episode in _owned_episodes(value, owner_id=owner_id):
        content = {key: episode.get(key) for key in ("summary", "subject", "episode")}
        if _contains_text(content, content_hint):
            return True
    return False


def _contains_text(value: Any, hint: str, *, depth: int = 0) -> bool:
    expected = hint.casefold()
    if not expected or depth > 6:
        return False
    if isinstance(value, str):
        return expected in value.casefold()
    if isinstance(value, dict):
        return any(_contains_text(item, hint, depth=depth + 1) for item in value.values())
    if isinstance(value, list):
        return any(_contains_text(item, hint, depth=depth + 1) for item in value)
    return False


def _contains_atomic_fact(value: Any, *, owner_id: str, fact_hint: str) -> bool:
    """Check the EverOS 1.1.3 public hybrid shape, not private storage details."""
    expected = fact_hint.casefold()
    for episode in _owned_episodes(value, owner_id=owner_id):
        facts = episode.get("atomic_facts")
        if not isinstance(facts, list):
            continue
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            fact_id = fact.get("id")
            content = fact.get("content")
            if isinstance(fact_id, str) and fact_id and isinstance(content, str) and expected in content.casefold():
                return True
    return False


def _owned_episodes(value: Any, *, owner_id: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, dict):
        return ()
    episodes = value.get("episodes")
    if not isinstance(episodes, list):
        return ()
    return tuple(
        episode
        for episode in episodes
        if isinstance(episode, dict) and episode.get("user_id") == owner_id
    )


def _storage_exists(everos_root: Path) -> bool:
    visible = any(everos_root.rglob("*.md"))
    sqlite = any((everos_root / ".index" / "sqlite").glob("*.db"))
    return visible and sqlite
