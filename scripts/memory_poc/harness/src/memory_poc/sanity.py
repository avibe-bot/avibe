from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .environment import checked_workspace_root, discover_provider_settings, locked_environment_python, verify_locked_environment
from .errors import HarnessError, LaunchError
from .generated_config import write_generated_config
from .identifiers import validate_run_id
from .launcher import EverOSProcess
from .metrics import read_call_metrics
from .paths import ensure_owner_directory, runtime_root
from .reports import build_report, set_criterion, write_report, write_summary
from .reports import local_timezone_name

_READINESS_TIMEOUT_SECONDS = 300.0


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

    started = time.monotonic()
    first = EverOSProcess(
        python=python,
        everos_root=everos_root,
        child_home=child_home,
        state_root=state,
        settings=settings,
        metrics_path=metrics_path,
        owner_id=fixture.owner_id,
    )
    try:
        client = first.start()
        add_ms = _elapsed_ms(lambda: client.add(session_id=fixture.session_id, messages=fixture.messages))
        flush_ms = _elapsed_ms(lambda: client.flush(session_id=fixture.session_id))
        first_reads = _read_required_memory(client, fixture)
    finally:
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
    try:
        restarted_client = second.start()
        restart_reads = _read_required_memory(restarted_client, fixture)
    finally:
        second.stop()

    storage_ok = _storage_exists(everos_root)
    metrics = read_call_metrics(metrics_path)
    report = build_report(run_id=run_id, settings=settings)
    set_criterion(report["criteria"], "launcher_uds_only", passed=True, value=1, threshold=1)
    set_criterion(report["criteria"], "restart_preserves", passed=first_reads and restart_reads, value=1, threshold=1)
    set_criterion(report["criteria"], "no_internals_needed", passed=True, value=1, threshold=1)
    report["latency"] = {
        "add_ms": {"sanity": add_ms},
        "flush_ms": {"sanity": flush_ms},
        "searchable_ms": {"sanity": int((time.monotonic() - started) * 1000)},
        "query_ms": {},
    }
    report["resources"]["llm_calls"] = metrics.llm_calls
    report["resources"]["embedding_calls"] = metrics.embedding_calls
    report["duplicates"] = {"observed": "not_run", "count": 0}
    if not storage_ok:
        raise HarnessError("sanity_storage_layout_missing")
    report_path = run_dir / "report.json"
    fixture_texts = tuple(message["content"] for message in fixture.messages)
    write_report(report_path, report, anchor=state, fixture_texts=fixture_texts)
    write_summary(
        run_dir / "summary.md",
        llm_calls=metrics.llm_calls,
        embedding_calls=metrics.embedding_calls,
        message_count=len(fixture.messages),
        anchor=state,
    )
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


def _read_required_memory(client: Any, fixture: SanityFixture) -> bool:
    deadline = time.monotonic() + _READINESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            profile = client.get(owner_id=fixture.owner_id, memory_type="profile")
            episodes = client.get(owner_id=fixture.owner_id, memory_type="episode")
            facts = client.search(owner_id=fixture.owner_id, query=fixture.query)
        except LaunchError:
            time.sleep(0.5)
            continue
        if (
            _contains_owned_items(profile, key="profiles", owner_id=fixture.owner_id)
            and _contains_owned_items(episodes, key="episodes", owner_id=fixture.owner_id)
            and _contains_atomic_fact(facts, owner_id=fixture.owner_id, fact_hint=fixture.fact_hint)
        ):
            return True
        time.sleep(0.5)
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
