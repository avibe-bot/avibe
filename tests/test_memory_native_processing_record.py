"""EverOS 1.2.3 native Processing Record contract tests."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from core.memory.native_processing_record import NativeProcessingRecordReader


PRINCIPAL = "u-" + "a" * 32
OTHER_PRINCIPAL = "u-" + "b" * 32
SESSION = "src--" + "1" * 64 + "--e0"
OTHER_SESSION = "src--" + "2" * 64 + "--e0"


def _reader(tmp_path: Path) -> NativeProcessingRecordReader:
    return NativeProcessingRecordReader(
        tmp_path / "everos",
        provider_base_urls=("https://provider.example/v1",),
        exact_redaction_values=("exact-secret",),
    )


def _system_db(tmp_path: Path) -> Path:
    path = tmp_path / "everos" / ".index" / "sqlite" / "system.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE memcell (
                memcell_id TEXT PRIMARY KEY,
                app_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                track TEXT NOT NULL,
                raw_type TEXT NOT NULL,
                message_ids_json TEXT NOT NULL,
                sender_ids_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );
            CREATE TABLE md_change_state (
                md_path TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                change_type TEXT NOT NULL,
                mtime REAL NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_changed_at TEXT NOT NULL,
                lsn INTEGER NOT NULL,
                status TEXT NOT NULL,
                retryable INTEGER,
                last_attempt_at TEXT,
                retry_count INTEGER NOT NULL,
                error TEXT
            );
            """
        )
    return path


def _ome_db(tmp_path: Path) -> Path:
    path = tmp_path / "everos" / ".index" / "sqlite" / "ome.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE run_record (
                run_id TEXT PRIMARY KEY,
                strategy_name TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                error TEXT,
                event_topic TEXT NOT NULL,
                event_payload TEXT NOT NULL,
                max_retries_snapshot INTEGER,
                event_id TEXT
            )
            """
        )
    return path


def _insert_memcell(
    db: Path,
    memcell_id: str,
    owner: str,
    *,
    session_id: str = SESSION,
    items: list[dict[str, object]] | None = None,
) -> None:
    items = items or [
        {
            "kind": "text",
            "id": f"message-{memcell_id}",
            "role": "user",
            "content": "hello",
            "timestamp": 1_700_000_000_000,
            "sender_id": owner,
        }
    ]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO memcell VALUES (?, 'avibe', 'default', ?, 'user', "
            "'message', ?, ?, ?, '2026-08-24T00:00:00.000Z')",
            (
                memcell_id,
                session_id,
                json.dumps([item.get("id") for item in items if item.get("id")]),
                json.dumps([owner]),
                json.dumps({"items": items}),
            ),
        )


def _insert_run(
    db: Path,
    run_id: str,
    *,
    memcell_id: str,
    session_id: str,
    error: str | None = None,
) -> None:
    payload = {
        "memcell_id": memcell_id,
        "app_id": "avibe",
        "project_id": "default",
        "session_id": session_id,
    }
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO run_record VALUES (?, 'extract_user_memory', 'failed', 2, "
            "'2026-08-24T00:00:01Z', '2026-08-24T00:00:02Z', ?, "
            "'everos.memory.UserPipelineStarted', ?, 3, ?)",
            (run_id, error, json.dumps(payload), f"event-{run_id}"),
        )


def _markdown(frontmatter: str, entries: str = "") -> str:
    return f"---\n{frontmatter.strip()}\n---\n{entries}"


def test_payload_preserves_authorized_boundaries_and_fails_closed(tmp_path: Path) -> None:
    system_db = _system_db(tmp_path)
    _ome_db(tmp_path)
    valid = {
        "kind": "text",
        "id": "message-valid",
        "role": "user",
        "content": [
            {"type": "text", "text": "first block"},
            {"type": "text", "text": "second block"},
        ],
        "timestamp": 1_700_000_000_000,
        "sender_id": PRINCIPAL,
    }
    _insert_memcell(
        system_db,
        "mc-authorized",
        PRINCIPAL,
        items=[
            valid,
            {**valid, "id": "message-foreign", "sender_id": OTHER_PRINCIPAL},
            {**valid, "id": "message-assistant", "role": "assistant"},
            {**valid, "id": "message-system", "role": "system"},
            {**valid, "id": "message-tool", "kind": "tool_call"},
        ],
    )
    _insert_memcell(system_db, "mc-foreign", OTHER_PRINCIPAL)

    reader = _reader(tmp_path)
    listed = reader.list_records((PRINCIPAL, "default"), None, 20)
    detail = reader.record_detail((PRINCIPAL, "default"), "mc-authorized")

    assert [entry["memcell_id"] for entry in listed["entries"]] == [
        "mc-authorized"
    ]
    assert detail["payload"]["status"] == "partial"
    assert detail["payload"]["omitted_count"] == 4
    assert detail["payload"]["items"] == [
        {
            "id": "message-valid",
            "timestamp_ms": 1_700_000_000_000,
            "sender_id": PRINCIPAL,
            "content": [
                {"type": "text", "text": "first block", "omitted_bytes": 0},
                {"type": "text", "text": "second block", "omitted_bytes": 0},
            ],
        }
    ]
    assert (
        reader.record_detail((PRINCIPAL, "default"), "mc-foreign")["status"]
        == "not_found"
    )


def test_runs_require_direct_native_scope_and_scrub_display_error(tmp_path: Path) -> None:
    system_db = _system_db(tmp_path)
    ome_db = _ome_db(tmp_path)
    _insert_memcell(system_db, "mc-1", PRINCIPAL)
    _insert_run(
        ome_db,
        "run-direct",
        memcell_id="mc-1",
        session_id=SESSION,
        error=(
            "Authorization: Bearer exact-secret at "
            "https://provider.example/v1/chat and /Users/private/input"
        ),
    )
    _insert_run(
        ome_db,
        "run-foreign-session",
        memcell_id="mc-1",
        session_id=OTHER_SESSION,
    )
    _insert_run(
        ome_db,
        "run-adjacent-only",
        memcell_id="mc-other",
        session_id=SESSION,
    )

    runs = _reader(tmp_path).record_detail(
        (PRINCIPAL, "default"), "mc-1"
    )["runs"]

    assert runs["status"] == "partial"
    assert [item["run_id"] for item in runs["items"]] == ["run-direct"]
    assert "exact-secret" not in runs["items"][0]["error"]
    assert "provider.example" not in runs["items"][0]["error"]
    assert "/Users/private" not in runs["items"][0]["error"]


def test_retained_away_runs_and_semantic_results_are_not_empty_success(
    tmp_path: Path,
) -> None:
    system_db = _system_db(tmp_path)
    _ome_db(tmp_path)
    _insert_memcell(system_db, "mc-1", PRINCIPAL)

    detail = _reader(tmp_path).record_detail(
        (PRINCIPAL, "default"), "mc-1"
    )

    assert detail["runs"] == {
        "status": "unavailable",
        "reason": "native_runs_missing_or_retained",
        "items": [],
    }
    assert detail["semantic"] == {
        "status": "unavailable",
        "reason": "semantic_results_missing_or_retained",
        "items": [],
    }


def test_oversized_native_message_metadata_is_unavailable_not_empty_success(
    tmp_path: Path,
) -> None:
    system_db = _system_db(tmp_path)
    _ome_db(tmp_path)
    _insert_memcell(system_db, "mc-1", PRINCIPAL)
    with sqlite3.connect(system_db) as conn:
        conn.execute(
            "UPDATE memcell SET message_ids_json = ? WHERE memcell_id = 'mc-1'",
            (json.dumps(["m" * 17_000]),),
        )

    payload = _reader(tmp_path).record_detail(
        (PRINCIPAL, "default"), "mc-1"
    )["payload"]

    assert payload == {
        "status": "unavailable",
        "reason": "payload_projection_limit",
        "items": [],
    }


def test_semantic_projection_uses_memcell_then_episode_link_and_current_is_unattributed(
    tmp_path: Path,
) -> None:
    system_db = _system_db(tmp_path)
    _ome_db(tmp_path)
    _insert_memcell(system_db, "mc-1", PRINCIPAL)
    owner_root = (
        tmp_path
        / "everos"
        / "avibe"
        / "default_project"
        / "users"
        / PRINCIPAL
    )
    episode = owner_root / "episodes" / "episode-2026-08-24.md"
    episode.parent.mkdir(parents=True)
    episode.write_text(
        _markdown(
            f"""
file_type: episode_daily
user_id: {PRINCIPAL}
track: user
""",
            f"""<!-- entry:ep_20260824_00000001 -->
## ep_20260824_00000001

**owner_id**: {PRINCIPAL}
**session_id**: {SESSION}
**timestamp**: 2026-08-24T00:00:02Z
**parent_type**: memcell
**parent_id**: mc-1

### Subject
Direct episode

### Summary
Stable linkage

### Content
Episode content
<!-- /entry:ep_20260824_00000001 -->
""",
        ),
        encoding="utf-8",
    )
    fact = owner_root / ".atomic_facts" / "atomic_fact-2026-08-24.md"
    fact.parent.mkdir(parents=True)
    fact.write_text(
        _markdown(
            f"""
file_type: atomic_fact_daily
user_id: {PRINCIPAL}
track: user
""",
            f"""<!-- entry:af_20260824_00000001 -->
## af_20260824_00000001

**owner_id**: {PRINCIPAL}
**session_id**: {SESSION}
**timestamp**: 2026-08-24T00:00:03Z
**parent_type**: episode
**parent_id**: ep_20260824_00000001

### Fact
Two-hop fact
<!-- /entry:af_20260824_00000001 -->
<!-- entry:af_20260824_00000002 -->
## af_20260824_00000002

**owner_id**: {PRINCIPAL}
**session_id**: {SESSION}
**timestamp**: 2026-08-24T00:00:04Z
**parent_type**: memcell
**parent_id**: mc-1

### Fact
Invalid direct fact
<!-- /entry:af_20260824_00000002 -->
""",
        ),
        encoding="utf-8",
    )
    profile = owner_root / "user.md"
    profile.write_text(
        _markdown(
            f"""
type: user_profile
user_id: {PRINCIPAL}
track: user
profile_timestamp_ms: 1700000000000
"""
        ),
        encoding="utf-8",
    )
    root = tmp_path / "everos"
    relative_paths = [
        episode.relative_to(root).as_posix(),
        fact.relative_to(root).as_posix(),
        profile.relative_to(root).as_posix(),
    ]
    with sqlite3.connect(system_db) as conn:
        conn.executemany(
            "INSERT INTO md_change_state VALUES (?, 'episode', 'modified', 1, "
            "'2026-08-24T00:00:00Z', '2026-08-24T00:00:05Z', 1, 'done', "
            "NULL, '2026-08-24T00:00:05Z', 0, NULL)",
            [(path,) for path in relative_paths],
        )

    detail = _reader(tmp_path).record_detail(
        (PRINCIPAL, "default"), "mc-1"
    )

    assert [(item["kind"], item["content"]) for item in detail["semantic"]["items"]] == [
        ("episode", "Episode content"),
        ("fact", "Two-hop fact"),
    ]
    assert detail["current_state"]["label"] == "current_unattributed"
    assert detail["current_state"]["profile"] == {
        "status": "present",
        "updated_at_ms": 1_700_000_000_000,
    }


def test_run_source_requires_the_real_everos_123_contract(tmp_path: Path) -> None:
    root = tmp_path / "everos"
    ome_db = root / ".index" / "sqlite" / "ome.db"
    ome_db.parent.mkdir(parents=True)
    with sqlite3.connect(ome_db) as conn:
        conn.execute("CREATE TABLE run_record (run_id TEXT PRIMARY KEY)")

    source = NativeProcessingRecordReader(root).source_observation().runs

    assert source.status == "unavailable"
    assert source.reason == "native_runs_unavailable"
