from __future__ import annotations

import json
import time
from dataclasses import dataclass
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
from .reports import build_report, set_criterion, write_report, write_summary
from .reports import local_timezone_name

_READINESS_TIMEOUT_SECONDS = 300.0
_READINESS_POLL_SECONDS = 0.5
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
    failure: HarnessError | None = None
    try:
        client = None
        try:
            client = first.start()
            first_started = True
            add_ms = _elapsed_ms(lambda: client.add(session_id=fixture.session_id, messages=fixture.messages))
            flush_started = time.monotonic()
            client.flush(session_id=fixture.session_id)
            flush_ms = int((time.monotonic() - flush_started) * 1000)
            first_searchable_ms = _read_required_memory(client, fixture, started_at=flush_started)
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
            _read_required_memory(restarted_client, fixture)
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


def _read_required_memory(client: Any, fixture: SanityFixture, *, started_at: float | None = None) -> int:
    measurement_start = time.monotonic() if started_at is None else started_at
    deadline = time.monotonic() + _READINESS_TIMEOUT_SECONDS
    next_search_at = measurement_start
    while time.monotonic() < deadline:
        try:
            profile = client.get(owner_id=fixture.owner_id, memory_type="profile")
            episodes = client.get(owner_id=fixture.owner_id, memory_type="episode")
        except LaunchError:
            time.sleep(_READINESS_POLL_SECONDS)
            continue
        profile_ready = _contains_owned_items(profile, key="profiles", owner_id=fixture.owner_id)
        episodes_ready = _contains_owned_items(episodes, key="episodes", owner_id=fixture.owner_id)
        now = time.monotonic()
        if profile_ready and episodes_ready and now >= next_search_at:
            try:
                facts = client.search(owner_id=fixture.owner_id, query=fixture.query)
            except LaunchError:
                time.sleep(_READINESS_POLL_SECONDS)
                continue
            if _contains_atomic_fact(facts, owner_id=fixture.owner_id, fact_hint=fixture.fact_hint):
                return int((time.monotonic() - measurement_start) * 1000)
            next_search_at = now + _SEARCH_POLL_SECONDS
        time.sleep(_READINESS_POLL_SECONDS)
    raise HarnessError("sanity_memory_not_ready")


def _contains_owned_items(value: Any, *, key: str, owner_id: str) -> bool:
    if not isinstance(value, dict):
        return False
    items = value.get(key)
    return isinstance(items, list) and any(
        isinstance(item, dict) and item.get("user_id") == owner_id for item in items
    )


def _contains_atomic_fact(value: Any, *, owner_id: str, fact_hint: str) -> bool:
    """Check the EverOS 1.1.3 public hybrid shape, not private storage details."""
    if not isinstance(value, dict):
        return False
    episodes = value.get("episodes")
    if not isinstance(episodes, list):
        return False
    expected = fact_hint.casefold()
    for episode in episodes:
        if not isinstance(episode, dict) or episode.get("user_id") != owner_id:
            continue
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


def _storage_exists(everos_root: Path) -> bool:
    visible = any(everos_root.rglob("*.md"))
    sqlite = any((everos_root / ".index" / "sqlite").glob("*.db"))
    return visible and sqlite
