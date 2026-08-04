from __future__ import annotations

import base64
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.memory.everos_insight import MemoryInsightPaths, MemoryInsightReader
from core.memory.everos_insight import reader as reader_module


ALICE = "u-" + "a" * 32
BOB = "u-" + "b" * 32
PROJECT = "p-" + "1" * 32
OTHER_PROJECT = "p-" + "2" * 32


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path)


@pytest.fixture
def insight_paths(tmp_path: Path) -> MemoryInsightPaths:
    root = tmp_path / "everos"
    system_db = root / ".index" / "sqlite" / "system.db"
    with _connect(system_db) as conn:
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
                timestamp TIMESTAMP NOT NULL
            );
            CREATE TABLE md_change_state (
                md_path TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                change_type TEXT NOT NULL,
                mtime REAL NOT NULL,
                first_seen_at TIMESTAMP NOT NULL,
                last_changed_at TIMESTAMP NOT NULL,
                lsn INTEGER NOT NULL,
                status TEXT NOT NULL,
                retryable INTEGER,
                last_attempt_at TIMESTAMP,
                retry_count INTEGER NOT NULL,
                error TEXT
            );
            """
        )

    ome_db = root / ".index" / "sqlite" / "ome.db"
    with _connect(ome_db) as conn:
        conn.executescript(
            """
            CREATE TABLE run_record (
                run_id TEXT PRIMARY KEY,
                strategy_name TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0,
                started_at TIMESTAMP NOT NULL,
                finished_at TIMESTAMP,
                error TEXT,
                event_topic TEXT NOT NULL,
                event_payload TEXT NOT NULL,
                max_retries_snapshot INTEGER NOT NULL,
                event_id TEXT NOT NULL
            );
            """
        )

    capture_db = tmp_path / "capture.db"
    with _connect(capture_db) as conn:
        conn.executescript(
            """
            CREATE TABLE memory_capture_queue (
                source_message_digest TEXT PRIMARY KEY,
                epoch INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                project_ref TEXT NOT NULL,
                provenance TEXT NOT NULL,
                payload_text TEXT,
                payload_attachments TEXT,
                occurred_at_ms INTEGER NOT NULL,
                provider_timestamp_ms INTEGER NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                next_retry_at TEXT,
                lease_owner TEXT,
                lease_at TEXT,
                last_error TEXT,
                add_request_id TEXT,
                flush_observation TEXT,
                flush_status TEXT,
                flush_error_code TEXT,
                flush_request_id TEXT,
                flush_observed_at TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT
            );
            """
        )

    call_db = tmp_path / "call-log.db"
    with _connect(call_db) as conn:
        conn.executescript(
            """
            CREATE TABLE provider_call (
                id TEXT PRIMARY KEY NOT NULL,
                started_at_ms INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL,
                kind TEXT NOT NULL,
                stage TEXT NOT NULL,
                model TEXT,
                status TEXT NOT NULL,
                error TEXT,
                finish_reason TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                request_json TEXT NOT NULL,
                response_json TEXT,
                request_bytes INTEGER NOT NULL,
                response_bytes INTEGER,
                request_id TEXT,
                strategy_name TEXT,
                run_id TEXT,
                attempt INTEGER,
                memcell_id TEXT,
                app_id TEXT,
                project_id TEXT,
                owner_id TEXT,
                md_path TEXT,
                entry_id TEXT,
                parent_type TEXT,
                parent_id TEXT,
                dropped_before INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX provider_call_request_id_idx ON provider_call(request_id);
            CREATE INDEX provider_call_run_id_idx ON provider_call(run_id);
            CREATE INDEX provider_call_memcell_id_idx ON provider_call(memcell_id);
            CREATE INDEX provider_call_parent_idx ON provider_call(parent_type, parent_id);
            """
        )
    return MemoryInsightPaths(root, capture_db, call_db)


def _insert_memcell(
    paths: MemoryInsightPaths,
    memcell_id: str,
    owner: object,
    *,
    timestamp_ms: int,
    project: str = PROJECT,
    message_ids: object | None = None,
    payload: object | None = None,
) -> None:
    value = payload or {
        "items": [
            {
                "role": "user",
                "sender_id": owner if isinstance(owner, str) else ALICE,
                "content": f"preview {memcell_id}",
            }
        ],
        "raw_secret": "never-project-this",
    }
    timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()
    with sqlite3.connect(paths.everos_root / ".index" / "sqlite" / "system.db") as conn:
        conn.execute(
            "INSERT INTO memcell VALUES (?, 'avibe', ?, 'session', 'user', 'message', ?, ?, ?, ?)",
            (
                memcell_id,
                project,
                _json(message_ids if message_ids is not None else []),
                _json(owner if isinstance(owner, list) else [owner]),
                _json(value),
                timestamp,
            ),
        )


def _insert_queue(
    paths: MemoryInsightPaths,
    digest: str,
    owner: str,
    *,
    session: str,
    timestamp_ms: int,
    project: str = PROJECT,
    add_request_id: str | None = None,
    flush_request_id: str | None = None,
) -> None:
    with sqlite3.connect(paths.capture_db_path) as conn:
        conn.execute(
            """
            INSERT INTO memory_capture_queue (
                source_message_digest, epoch, session_id, principal_id,
                project_ref, provenance, occurred_at_ms, provider_timestamp_ms,
                state, attempts, add_request_id, flush_request_id, created_at,
                completed_at
            ) VALUES (?, 1, ?, ?, ?, 'user_input', ?, ?, 'delivered', 1, ?, ?, ?, ?)
            """,
            (
                digest,
                session,
                owner,
                project,
                timestamp_ms,
                timestamp_ms,
                add_request_id,
                flush_request_id,
                "2026-08-04T00:00:00Z",
                "2026-08-04T00:00:01Z",
            ),
        )


def _insert_run(
    paths: MemoryInsightPaths,
    run_id: str,
    strategy: str,
    event: object,
    *,
    started_at: str = "2026-08-04T00:00:02+00:00",
    status: str = "success",
    error: str | None = None,
    topic: str = "everos.memory.events:EpisodeExtracted",
) -> None:
    finished_at = None if status == "running" else "2026-08-04T00:00:03+00:00"
    with sqlite3.connect(paths.everos_root / ".index" / "sqlite" / "ome.db") as conn:
        conn.execute(
            "INSERT INTO run_record VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, 2, 'event')",
            (
                run_id,
                strategy,
                status,
                started_at,
                finished_at,
                error,
                topic,
                _json(event) if not isinstance(event, str) else event,
            ),
        )


def _insert_call(paths: MemoryInsightPaths, call_id: str, **values: object) -> None:
    columns = {
        "id": call_id,
        "started_at_ms": 1_722_816_004_000,
        "duration_ms": 12,
        "kind": "llm",
        "stage": "strategy",
        "model": "model",
        "status": "success",
        "error": None,
        "finish_reason": "stop",
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "request_json": _json({"prompt": "hello"}),
        "response_json": _json({"answer": "world"}),
        "request_bytes": 18,
        "response_bytes": 18,
        "request_id": None,
        "strategy_name": None,
        "run_id": None,
        "attempt": None,
        "memcell_id": None,
        "app_id": None,
        "project_id": None,
        "owner_id": None,
        "md_path": None,
        "entry_id": None,
        "parent_type": None,
        "parent_id": None,
        "dropped_before": 0,
    }
    columns.update(values)
    names = list(columns)
    with sqlite3.connect(paths.call_log_db_path) as conn:
        conn.execute(
            f"INSERT INTO provider_call ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
            tuple(columns[name] for name in names),
        )


def test_list_is_owner_scoped_and_omits_malformed_or_multi_owner(
    insight_paths: MemoryInsightPaths,
) -> None:
    _insert_memcell(insight_paths, "mc_alice", ALICE, timestamp_ms=3_000)
    _insert_memcell(insight_paths, "mc_bob", BOB, timestamp_ms=2_000)
    _insert_memcell(insight_paths, "mc_multi", [ALICE, BOB], timestamp_ms=4_000)
    _insert_memcell(insight_paths, "mc_wrong_project", ALICE, timestamp_ms=5_000, project=OTHER_PROJECT)
    _insert_memcell(insight_paths, "mc_bad", ALICE, timestamp_ms=6_000)
    with sqlite3.connect(insight_paths.everos_root / ".index" / "sqlite" / "system.db") as conn:
        conn.execute("UPDATE memcell SET sender_ids_json='not-json' WHERE memcell_id='mc_bad'")

    reader = MemoryInsightReader(insight_paths)
    assert [row["memcell_id"] for row in reader.list_entries((ALICE, PROJECT), None, 50)["entries"]] == [
        "mc_alice"
    ]
    assert [row["memcell_id"] for row in reader.list_entries((BOB, PROJECT), None, 50)["entries"]] == [
        "mc_bob"
    ]
    assert reader.entry_detail((BOB, PROJECT), "mc_alice") == {"status": "not_found"}


def test_cursor_is_canonical_and_orders_duplicate_timestamps(
    insight_paths: MemoryInsightPaths,
) -> None:
    for memcell_id, timestamp in (("mc_c", 3_000), ("mc_b", 2_000), ("mc_a", 2_000)):
        _insert_memcell(insight_paths, memcell_id, ALICE, timestamp_ms=timestamp)
    reader = MemoryInsightReader(insight_paths)

    first = reader.list_entries((ALICE, PROJECT), None, 2)
    assert [item["memcell_id"] for item in first["entries"]] == ["mc_c", "mc_b"]
    second = reader.list_entries((ALICE, PROJECT), first["next_cursor"], 2)
    assert [item["memcell_id"] for item in second["entries"]] == ["mc_a"]
    assert second["next_cursor"] is None

    malformed = ["!", "a" * 89, base64.urlsafe_b64encode(b"{}").decode().rstrip("=")]
    for cursor in malformed:
        with pytest.raises(ValueError, match="cursor"):
            reader.list_entries((ALICE, PROJECT), cursor, 2)


def test_cursor_truncates_submillisecond_timestamps_before_ordering(
    insight_paths: MemoryInsightPaths,
) -> None:
    _insert_memcell(insight_paths, "mc_z", ALICE, timestamp_ms=2_000)
    _insert_memcell(insight_paths, "mc_a", ALICE, timestamp_ms=2_000)
    with sqlite3.connect(insight_paths.system_db_path) as conn:
        conn.execute(
            "UPDATE memcell SET timestamp = ? WHERE memcell_id = 'mc_z'",
            ("1970-01-01T00:00:02.000400+00:00",),
        )
        conn.execute(
            "UPDATE memcell SET timestamp = ? WHERE memcell_id = 'mc_a'",
            ("1970-01-01T00:00:02.000600+00:00",),
        )

    reader = MemoryInsightReader(insight_paths)
    first = reader.list_entries((ALICE, PROJECT), None, 1)
    second = reader.list_entries((ALICE, PROJECT), first["next_cursor"], 1)

    assert [(entry["memcell_id"], entry["timestamp_ms"]) for entry in first["entries"]] == [
        ("mc_z", 2_000)
    ]
    assert [(entry["memcell_id"], entry["timestamp_ms"]) for entry in second["entries"]] == [
        ("mc_a", 2_000)
    ]
    assert second["next_cursor"] is None


def test_cursor_accepts_production_length_provider_message_ids(
    insight_paths: MemoryInsightPaths,
) -> None:
    production_id = f"m_src--{'a' * 64}--e1_1722816000000_000"
    _insert_memcell(insight_paths, production_id, ALICE, timestamp_ms=3_000)
    _insert_memcell(insight_paths, "mc_older", ALICE, timestamp_ms=2_000)
    reader = MemoryInsightReader(insight_paths)

    first = reader.list_entries((ALICE, PROJECT), None, 1)
    assert first["entries"][0]["memcell_id"] == production_id
    assert isinstance(first["next_cursor"], str)
    assert 88 < len(first["next_cursor"]) <= 256
    second = reader.list_entries((ALICE, PROJECT), first["next_cursor"], 1)
    assert [entry["memcell_id"] for entry in second["entries"]] == ["mc_older"]


def test_list_page_bounds_history_before_python_projection(
    insight_paths: MemoryInsightPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_counts: dict[str, int] = {}
    for index in range(50):
        memcell_id = f"mc_visible_{index:02d}"
        _insert_memcell(insight_paths, memcell_id, ALICE, timestamp_ms=10_000 + index)
        expected_counts[memcell_id] = index % 3
        for call_index in range(index % 3):
            _insert_call(
                insight_paths,
                f"call-visible-{index:02d}-{call_index}",
                memcell_id=memcell_id,
            )
    for index in range(75):
        memcell_id = f"mc_foreign_{index:02d}"
        _insert_memcell(insight_paths, memcell_id, BOB, timestamp_ms=20_000 + index)
        _insert_run(
            insight_paths,
            f"run-foreign-{index:02d}",
            "extract_atomic_facts",
            {
                "memcell_id": memcell_id,
                "app_id": "avibe",
                "project_id": PROJECT,
                "owner_id": BOB,
            },
        )
        _insert_call(
            insight_paths,
            f"call-foreign-{index:02d}",
            memcell_id=memcell_id,
            app_id="avibe",
            project_id=PROJECT,
            owner_id=BOB,
        )

    statements: list[str] = []
    original_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)

    result = MemoryInsightReader(insight_paths).list_entries((ALICE, PROJECT), None, 50)

    assert {
        entry["memcell_id"]: entry["authorized_call_count"] for entry in result["entries"]
    } == expected_counts
    assert {entry["memcell_id"]: entry["run_summary"] for entry in result["entries"]} == {
        memcell_id: {"total": 0, "statuses": {}} for memcell_id in expected_counts
    }
    selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(("SELECT", "WITH"))
    ]
    assert len(selects) == 4
    assert "LIMIT 51" in selects[0]
    assert "COUNT(*) AS total" in selects[2]
    assert " UNION " in selects[3]

    with original_connect(insight_paths.call_log_db_path) as conn:
        conn.execute("ATTACH DATABASE ? AS capture", (str(insight_paths.capture_db_path),))
        conn.execute("ATTACH DATABASE ? AS ome", (str(insight_paths.ome_db_path),))
        query_plan = "\n".join(
            str(row[3]) for row in conn.execute(f"EXPLAIN QUERY PLAN {selects[3]}")
        )
    for index_name in (
        "provider_call_memcell_id_idx",
        "provider_call_request_id_idx",
        "provider_call_run_id_idx",
        "provider_call_parent_idx",
    ):
        assert f"SEARCH pc USING INDEX {index_name}" in query_plan
    assert "SCAN pc" not in query_plan


def test_exact_capture_and_request_group_authorization(insight_paths: MemoryInsightPaths) -> None:
    message_id = "m_session-a_5000_000"
    _insert_memcell(
        insight_paths,
        "mc_capture",
        ALICE,
        timestamp_ms=5_100,
        message_ids=[message_id],
    )
    _insert_queue(
        insight_paths,
        "alice",
        ALICE,
        session="session-a",
        timestamp_ms=5_000,
        add_request_id="request-good",
        flush_request_id="flush-mixed",
    )
    _insert_queue(
        insight_paths,
        "bob",
        BOB,
        session="session-b",
        timestamp_ms=5_001,
        flush_request_id="flush-mixed",
    )
    _insert_call(insight_paths, "good", request_id="request-good", stage="boundary")
    _insert_call(insight_paths, "mixed", request_id="flush-mixed", stage="boundary")
    _insert_call(insight_paths, "unlinked", request_id="request-other", stage="boundary")

    detail = MemoryInsightReader(insight_paths).entry_detail((ALICE, PROJECT), "mc_capture")
    assert [call["id"] for call in detail["calls"]] == ["good"]
    assert detail["capture"]["status"] == "available"
    listed = MemoryInsightReader(insight_paths).list_entries((ALICE, PROJECT), None, 10)
    assert listed["entries"][0]["authorized_call_count"] == 1
    encoded = json.dumps(detail)
    assert "flush-mixed" not in encoded
    assert "request-other" not in encoded


def test_capture_message_id_collision_with_foreign_scope_fails_closed(
    insight_paths: MemoryInsightPaths,
) -> None:
    message_id = "m_collision_6100_000"
    _insert_memcell(
        insight_paths,
        "mc_collision",
        ALICE,
        timestamp_ms=6_200,
        message_ids=[message_id],
    )
    _insert_queue(
        insight_paths,
        "foreign-collision",
        BOB,
        session="collision",
        timestamp_ms=6_100,
        add_request_id="foreign-request",
    )
    _insert_call(insight_paths, "foreign-boundary", request_id="foreign-request")

    detail = MemoryInsightReader(insight_paths).entry_detail((ALICE, PROJECT), "mc_collision")
    assert detail["capture"] == {"status": "unavailable", "reason": "expired"}
    assert detail["calls"] == []


def test_oversized_memcell_json_is_not_decoded_or_used_for_capture_attribution(
    insight_paths: MemoryInsightPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memcell_id = "mc_bounded_json"
    message_id = "m_bounded_6400_000"
    _insert_memcell(
        insight_paths,
        memcell_id,
        ALICE,
        timestamp_ms=6_500,
        message_ids=[message_id],
    )
    oversized_payload = _json(
        {
            "items": [
                {
                    "role": "user",
                    "sender_id": ALICE,
                    "content": "x" * reader_module._MAX_MEMCELL_PAYLOAD_JSON_BYTES,
                }
            ]
        }
    )
    oversized_message_ids = _json(
        [message_id, "x" * reader_module._MAX_MEMCELL_MESSAGE_IDS_JSON_BYTES]
    )
    oversized_senders = _json(
        [ALICE, "x" * reader_module._MAX_MEMCELL_SENDER_IDS_JSON_BYTES]
    )
    assert len(oversized_payload.encode()) > reader_module._MAX_MEMCELL_PAYLOAD_JSON_BYTES
    assert (
        len(oversized_message_ids.encode())
        > reader_module._MAX_MEMCELL_MESSAGE_IDS_JSON_BYTES
    )
    assert len(oversized_senders.encode()) > reader_module._MAX_MEMCELL_SENDER_IDS_JSON_BYTES
    _insert_memcell(insight_paths, "mc_oversized_senders", ALICE, timestamp_ms=6_400)
    with sqlite3.connect(insight_paths.system_db_path) as conn:
        conn.execute(
            """
            UPDATE memcell
            SET payload_json = ?, message_ids_json = ?
            WHERE memcell_id = ?
            """,
            (oversized_payload, oversized_message_ids, memcell_id),
        )
        conn.execute(
            "UPDATE memcell SET sender_ids_json = ? WHERE memcell_id = ?",
            (oversized_senders, "mc_oversized_senders"),
        )

    _insert_queue(
        insight_paths,
        "bounded",
        ALICE,
        session="bounded",
        timestamp_ms=6_400,
        add_request_id="capture-request",
    )
    _insert_run(
        insight_paths,
        "run-bounded",
        "extract_atomic_facts",
        {
            "memcell_id": memcell_id,
            "app_id": "avibe",
            "project_id": PROJECT,
            "owner_id": ALICE,
        },
    )
    _insert_call(insight_paths, "direct-bounded", memcell_id=memcell_id)
    _insert_call(insight_paths, "run-bounded-call", run_id="run-bounded")
    _insert_call(insight_paths, "capture-bounded-call", request_id="capture-request")

    original_decode = reader_module._decode_json
    decoded_values: list[str] = []

    def guarded_decode(value: object) -> object:
        if isinstance(value, str):
            decoded_values.append(value)
            assert value not in {
                oversized_payload,
                oversized_message_ids,
                oversized_senders,
            }
        return original_decode(value)

    monkeypatch.setattr(reader_module, "_decode_json", guarded_decode)
    reader = MemoryInsightReader(insight_paths)
    listed = reader.list_entries((ALICE, PROJECT), None, 10)
    detail = reader.entry_detail((ALICE, PROJECT), memcell_id)

    entry = next(item for item in listed["entries"] if item["memcell_id"] == memcell_id)
    assert entry["preview"] == ""
    assert entry["message_count"] == 0
    assert entry["authorized_call_count"] == 2
    assert detail["entry"]["preview"] == ""
    assert detail["entry"]["message_count"] == 0
    assert detail["capture"] == {"status": "unavailable", "reason": "expired"}
    assert {call["id"] for call in detail["calls"]} == {
        "direct-bounded",
        "run-bounded-call",
    }
    assert decoded_values


def test_expired_queue_link_is_explicit_and_has_no_fallback(
    insight_paths: MemoryInsightPaths,
) -> None:
    _insert_memcell(
        insight_paths,
        "mc_expired",
        ALICE,
        timestamp_ms=7_000,
        message_ids=["m_expired-session_6999_000"],
    )
    _insert_call(insight_paths, "shared", request_id="shared-flush", stage="boundary")

    detail = MemoryInsightReader(insight_paths).entry_detail((ALICE, PROJECT), "mc_expired")
    assert detail["capture"] == {"status": "unavailable", "reason": "expired"}
    assert detail["calls"] == []


def test_run_owner_is_authoritative_and_ownerless_event_falls_back(
    insight_paths: MemoryInsightPaths,
) -> None:
    _insert_memcell(insight_paths, "mc_run", ALICE, timestamp_ms=8_000)
    base = {"memcell_id": "mc_run", "app_id": "avibe", "project_id": PROJECT}
    _insert_run(insight_paths, "run-mismatch", "extract_atomic_facts", {**base, "owner_id": BOB})
    _insert_run(insight_paths, "run-ownerless", "extract_atomic_facts", base)
    _insert_run(insight_paths, "run-profile", "extract_user_profile", {**base, "owner_id": ALICE})
    _insert_run(insight_paths, "run-bad-json", "extract_atomic_facts", "not-json")
    _insert_call(insight_paths, "ownerless-call", run_id="run-ownerless")
    _insert_call(insight_paths, "mismatch-call", run_id="run-mismatch")

    detail = MemoryInsightReader(insight_paths).entry_detail((ALICE, PROJECT), "mc_run")
    assert {step["run_id"] for step in detail["steps"] if "run_id" in step} == {
        "run-ownerless",
        "run-profile",
    }
    profile_step = next(step for step in detail["steps"] if step.get("run_id") == "run-profile")
    assert profile_step["relation"] == "profile_trigger"
    assert [call["id"] for call in detail["calls"]] == ["ownerless-call"]

    listed = MemoryInsightReader(insight_paths).list_entries((ALICE, PROJECT), None, 10)
    assert listed["entries"][0]["run_summary"] == {
        "total": 2,
        "statuses": {"success": 2},
    }
    assert listed["entries"][0]["authorized_call_count"] == 1


def test_direct_and_atomic_fact_cascades_require_exact_scope(
    insight_paths: MemoryInsightPaths,
) -> None:
    _insert_memcell(insight_paths, "mc_cascade", ALICE, timestamp_ms=9_000)
    event = {
        "memcell_id": "mc_cascade",
        "episode_entry_id": "episode-1",
        "app_id": "avibe",
        "project_id": PROJECT,
        "owner_id": ALICE,
    }
    _insert_run(insight_paths, "run-episode", "extract_atomic_facts", event)
    _insert_run(
        insight_paths,
        "run-wrong-topic-case",
        "extract_atomic_facts",
        {**event, "episode_entry_id": "episode-wrong-topic-case"},
        topic="everos.memory.events:episodeextracted",
    )
    _insert_call(
        insight_paths,
        "direct",
        stage="cascade",
        app_id="avibe",
        project_id=PROJECT,
        owner_id=ALICE,
        parent_type="memcell",
        parent_id="mc_cascade",
    )
    _insert_call(
        insight_paths,
        "atomic",
        stage="cascade",
        app_id="avibe",
        project_id=PROJECT,
        owner_id=ALICE,
        parent_type="episode",
        parent_id="episode-1",
    )
    _insert_call(
        insight_paths,
        "foreign",
        stage="cascade",
        app_id="avibe",
        project_id=PROJECT,
        owner_id=BOB,
        parent_type="episode",
        parent_id="episode-1",
    )
    _insert_call(
        insight_paths,
        "wrong-topic-case",
        stage="cascade",
        app_id="avibe",
        project_id=PROJECT,
        owner_id=ALICE,
        parent_type="episode",
        parent_id="episode-wrong-topic-case",
    )

    detail = MemoryInsightReader(insight_paths).entry_detail((ALICE, PROJECT), "mc_cascade")
    assert {call["id"] for call in detail["calls"]} == {"direct", "atomic"}
    listed = MemoryInsightReader(insight_paths).list_entries((ALICE, PROJECT), None, 10)
    assert listed["entries"][0]["authorized_call_count"] == 2


def test_projection_scrubs_and_never_returns_raw_payload_or_paths(
    insight_paths: MemoryInsightPaths,
) -> None:
    secret = "sk-1234567890abcdef"
    raw_path = "/Users/alice/private/secret.txt"
    payload = {
        "items": [
            {
                "role": "user",
                "sender_id": ALICE,
                "content": [
                    {"type": "text", "text": f"hello {secret} {raw_path}"},
                    {"type": "image", "name": raw_path, "uri": "file:///private/image.png"},
                ],
            },
            {
                "role": "user",
                "sender_id": BOB,
                "content": "corrupt-foreign-preview",
            },
        ],
        "raw_payload_marker": "must-not-leak",
    }
    _insert_memcell(insight_paths, "mc_safe", ALICE, timestamp_ms=10_000, payload=payload)
    _insert_run(
        insight_paths,
        "run-safe",
        "extract_atomic_facts",
        {
            "memcell_id": "mc_safe",
            "app_id": "avibe",
            "project_id": PROJECT,
            "owner_id": ALICE,
            "event_secret": "must-not-leak-event",
        },
        status="failed",
        error=f"Authorization: Bearer {secret} at {raw_path}",
    )

    result = MemoryInsightReader(insight_paths).entry_detail((ALICE, PROJECT), "mc_safe")
    encoded = json.dumps(result)
    assert "must-not-leak" not in encoded
    assert "corrupt-foreign-preview" not in encoded
    assert secret not in encoded
    assert raw_path not in encoded
    assert "file:///private" not in encoded
    assert "[REDACTED]" in encoded
    assert "[LOCAL_PATH]" in encoded


def test_preview_includes_every_supported_document_attachment_kind(
    insight_paths: MemoryInsightPaths,
) -> None:
    payload = {
        "items": [
            {
                "role": "user",
                "sender_id": ALICE,
                "content": [
                    {"type": "doc", "name": "notes.txt"},
                    {"type": "pdf", "name": "report.pdf"},
                    {"type": "html", "name": "page.html"},
                    {"type": "email", "name": "message.eml"},
                ],
            }
        ]
    }
    _insert_memcell(
        insight_paths,
        "mc_document_attachments",
        ALICE,
        timestamp_ms=10_250,
        payload=payload,
    )

    result = MemoryInsightReader(insight_paths).list_entries((ALICE, PROJECT), None, 10)

    assert result["entries"][0]["preview"] == (
        "[doc: notes.txt] [pdf: report.pdf] [html: page.html] [email: message.eml]"
    )


def test_configured_provider_urls_are_scrubbed_from_runs_and_calls(
    insight_paths: MemoryInsightPaths,
) -> None:
    provider_url = "https://llm.internal.example/v1"
    _insert_memcell(insight_paths, "mc_url", ALICE, timestamp_ms=10_500)
    event = {
        "memcell_id": "mc_url",
        "app_id": "avibe",
        "project_id": PROJECT,
        "owner_id": ALICE,
    }
    _insert_run(
        insight_paths,
        "run-url",
        "extract_atomic_facts",
        event,
        status="failed",
        error=f"provider failed at {provider_url}/chat",
    )
    _insert_call(
        insight_paths,
        "call-url",
        memcell_id="mc_url",
        request_json=_json({"endpoint": f"{provider_url}/chat"}),
    )

    detail = MemoryInsightReader(
        insight_paths,
        provider_base_urls=(provider_url,),
    ).entry_detail((ALICE, PROJECT), "mc_url")
    encoded = json.dumps(detail)
    assert "llm.internal.example" not in encoded
    assert "[PROVIDER_BASE_URL]" in encoded


def test_provider_url_redaction_normalizes_scheme_and_host(
    insight_paths: MemoryInsightPaths,
) -> None:
    configured_url = "HTTPS://LLM.Internal.Example/v1"
    echoed_url = "https://llm.internal.example/v1/chat"
    _insert_memcell(insight_paths, "mc_url_case", ALICE, timestamp_ms=10_500)
    _insert_call(
        insight_paths,
        "call-url-case",
        memcell_id="mc_url_case",
        error=f"provider failed at {echoed_url}",
    )

    detail = MemoryInsightReader(
        insight_paths,
        provider_base_urls=(configured_url,),
    ).entry_detail((ALICE, PROJECT), "mc_url_case")

    assert "llm.internal.example" not in json.dumps(detail).casefold()
    assert detail["calls"][0]["error"] == "provider failed at [PROVIDER_BASE_URL]/chat"


def test_detail_preserves_internal_ids_and_enums_when_exact_key_is_short(
    insight_paths: MemoryInsightPaths,
) -> None:
    _insert_memcell(insight_paths, "mc-u-1", ALICE, timestamp_ms=10_500)
    event = {"memcell_id": "mc-u-1", "app_id": "avibe", "project_id": PROJECT, "owner_id": ALICE}
    _insert_run(insight_paths, "run-u-1", "extract_user_profile", event, status="dead_letter")
    _insert_call(
        insight_paths,
        "call-u-1",
        memcell_id="mc-u-1",
        kind="multimodal_llm",
        stage="episode_extract",
        status="crashed",
        error="provider echoed u",
    )

    detail = MemoryInsightReader(insight_paths, exact_redaction_values=("u",)).entry_detail(
        (ALICE, PROJECT), "mc-u-1"
    )

    assert detail["entry"]["memcell_id"] == "mc-u-1"
    strategy = next(step for step in detail["steps"] if step["type"] == "strategy")
    assert strategy["run_id"] == "run-u-1"
    assert strategy["strategy"] == "extract_user_profile"
    assert strategy["status"] == "dead_letter"
    assert detail["calls"][0]["id"] == "call-u-1"
    assert detail["calls"][0]["kind"] == "multimodal_llm"
    assert detail["calls"][0]["stage"] == "episode_extract"
    assert detail["calls"][0]["status"] == "crashed"
    assert detail["calls"][0]["error"] == "provider echoed [REDACTED]"


def test_detail_call_from_an_older_omitted_run_remains_authorized(
    insight_paths: MemoryInsightPaths,
) -> None:
    _insert_memcell(insight_paths, "mc_many_runs", ALICE, timestamp_ms=10_550)
    event = {
        "memcell_id": "mc_many_runs",
        "app_id": "avibe",
        "project_id": PROJECT,
        "owner_id": ALICE,
    }
    for index in range(51):
        _insert_run(
            insight_paths,
            f"run-{index:02d}",
            "extract_atomic_facts",
            event,
            started_at=f"2026-08-04T00:{index:02d}:00+00:00",
        )
    _insert_call(
        insight_paths,
        "call-from-oldest-run",
        run_id="run-00",
        started_at_ms=1_900_000_000_000,
    )

    detail = MemoryInsightReader(insight_paths).entry_detail(
        (ALICE, PROJECT), "mc_many_runs"
    )

    displayed_run_ids = {step.get("run_id") for step in detail["steps"]}
    assert "run-00" not in displayed_run_ids
    assert [call["id"] for call in detail["calls"]] == ["call-from-oldest-run"]
    assert detail["omitted_step_count"] == 1


def test_configured_provider_keys_are_scrubbed_from_run_and_current_state_errors(
    insight_paths: MemoryInsightPaths,
) -> None:
    llm_key = "opaqueLLMCredentialValue937"
    embedding_key = f"{llm_key}EmbeddingSuffix"
    _insert_memcell(insight_paths, "mc_exact_keys", ALICE, timestamp_ms=10_600)
    event = {
        "memcell_id": "mc_exact_keys",
        "app_id": "avibe",
        "project_id": PROJECT,
        "owner_id": ALICE,
    }
    _insert_run(
        insight_paths,
        "run-exact-keys",
        "extract_user_profile",
        event,
        status="failed",
        error=f"LLM failed: {llm_key}; embedding: {embedding_key}",
    )
    with sqlite3.connect(insight_paths.system_db_path) as conn:
        conn.execute(
            """
            INSERT INTO md_change_state (
                md_path, kind, change_type, mtime, first_seen_at,
                last_changed_at, lsn, status, retryable, last_attempt_at,
                retry_count, error
            ) VALUES (?, 'user_profile', 'modified', 1, ?, ?, 1, 'failed', 1, ?, 1, ?)
            """,
            (
                f"avibe/{PROJECT}/users/{ALICE}/user.md",
                "2026-08-04T00:00:01+00:00",
                "2026-08-04T00:00:02+00:00",
                "2026-08-04T00:00:03+00:00",
                f"indexing failed with {embedding_key} and {llm_key}",
            ),
        )

    detail = MemoryInsightReader(
        insight_paths,
        exact_redaction_values=(llm_key, embedding_key),
    ).entry_detail((ALICE, PROJECT), "mc_exact_keys")

    run = next(step for step in detail["steps"] if step.get("run_id") == "run-exact-keys")
    assert run["error"] == "LLM failed: [REDACTED]; embedding: [REDACTED]"
    assert detail["current_state"]["indexing"]["error"] == (
        "indexing failed with [REDACTED] and [REDACTED]"
    )
    encoded = json.dumps(detail)
    assert llm_key not in encoded
    assert embedding_key not in encoded


def test_missing_sources_degrade_independently(tmp_path: Path) -> None:
    root = tmp_path / "missing"
    reader = MemoryInsightReader(
        MemoryInsightPaths(root, tmp_path / "missing-capture.db", tmp_path / "missing-call.db")
    )
    result = reader.list_entries((ALICE, PROJECT), None, 10)
    assert result["status"] == "ok"
    assert result["entries"] == []
    assert result["sections"] == {
        "everos": {"status": "unavailable", "reason": "missing"},
        "capture": {"status": "unavailable", "reason": "missing"},
        "calls": {"status": "unavailable", "reason": "missing"},
    }


def test_locked_optional_db_does_not_fail_everos_result(insight_paths: MemoryInsightPaths) -> None:
    lock = sqlite3.connect(insight_paths.capture_db_path, isolation_level=None)
    lock.execute("PRAGMA journal_mode=DELETE")
    lock.execute("BEGIN EXCLUSIVE")
    try:
        _insert_memcell(insight_paths, "mc_locked", ALICE, timestamp_ms=11_000)
        result = MemoryInsightReader(insight_paths).list_entries((ALICE, PROJECT), None, 10)
    finally:
        lock.rollback()
        lock.close()
    assert [entry["memcell_id"] for entry in result["entries"]] == ["mc_locked"]
    assert result["sections"]["capture"] == {"status": "unavailable", "reason": "busy"}


def test_list_keeps_direct_call_count_when_run_table_is_unavailable(
    insight_paths: MemoryInsightPaths,
) -> None:
    _insert_memcell(insight_paths, "mc_partial", ALICE, timestamp_ms=11_500)
    _insert_call(insight_paths, "call-partial", memcell_id="mc_partial")
    with sqlite3.connect(insight_paths.ome_db_path) as conn:
        conn.execute("DROP TABLE run_record")

    result = MemoryInsightReader(insight_paths).list_entries((ALICE, PROJECT), None, 10)

    assert result["sections"]["everos"] == {"status": "partial", "reason": "runs_malformed"}
    assert result["entries"][0]["run_summary"] is None
    assert result["entries"][0]["authorized_call_count"] == 1


def test_list_keeps_direct_call_count_when_capture_table_is_malformed(
    insight_paths: MemoryInsightPaths,
) -> None:
    _insert_memcell(insight_paths, "mc_malformed_capture", ALICE, timestamp_ms=11_600)
    _insert_call(insight_paths, "call-malformed-capture", memcell_id="mc_malformed_capture")
    with sqlite3.connect(insight_paths.capture_db_path) as conn:
        conn.execute("DROP TABLE memory_capture_queue")
        conn.execute("CREATE TABLE memory_capture_queue (session_id TEXT)")

    result = MemoryInsightReader(insight_paths).list_entries((ALICE, PROJECT), None, 10)

    assert result["sections"]["capture"] == {"status": "unavailable", "reason": "malformed"}
    assert result["sections"]["calls"] == {"status": "available"}
    assert result["entries"][0]["authorized_call_count"] == 1


def test_detail_has_fixed_bounds_omission_counts_and_response_ceiling(
    insight_paths: MemoryInsightPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_memcell(insight_paths, "mc_large", ALICE, timestamp_ms=12_000)
    huge = "\\\"" * 20_000
    for index in range(55):
        _insert_run(
            insight_paths,
            f"run-{index:02d}",
            "extract_atomic_facts",
            {
                "memcell_id": "mc_large",
                "app_id": "avibe",
                "project_id": PROJECT,
                "owner_id": ALICE,
            },
            started_at=f"2026-08-04T00:{index:02d}:00+00:00",
            status="failed",
            error=huge,
        )
    for index in range(25):
        _insert_call(
            insight_paths,
            f"call-{index:02d}",
            started_at_ms=1_722_816_000_000 + index,
            memcell_id="mc_large",
            request_json=_json({"prompt": huge}),
            response_json=_json({"answer": huge}),
            request_bytes=len(huge),
            response_bytes=len(huge),
            error=huge,
        )

    statements: list[str] = []
    original_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)

    detail = MemoryInsightReader(insight_paths).entry_detail((ALICE, PROJECT), "mc_large")
    assert len([step for step in detail["steps"] if "run_id" in step]) == 50
    assert detail["omitted_step_count"] == 5
    assert len(detail["calls"]) == 20
    assert detail["omitted_call_count"] == 5
    for call in detail["calls"]:
        assert len(_json(call["request"]).encode()) <= 12_000
        assert len(_json(call["response"]).encode()) <= 12_000
        assert len(_json(call["error"]).encode()) <= 1_024
        assert set(call["request"]) == {"excerpt", "omitted_bytes"}
        assert set(call["response"]) == {"excerpt", "omitted_bytes"}
    assert len(json.dumps(detail, ensure_ascii=False, separators=(",", ":")).encode()) <= 1_000_000

    detail_run_query = next(
        statement for statement in statements if "selected_runs AS MATERIALIZED" in statement
    )
    detail_call_query = next(
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("WITH")
        and "selected_calls AS MATERIALIZED" in statement
    )
    capture_query = next(
        statement
        for statement in statements
        if "FROM memory_capture_queue" in statement
        and "json_each(" in statement
        and "provider_timestamp_ms" in statement
    )
    assert "LIMIT 50" in detail_run_query
    assert "LIMIT 20" in detail_call_query
    assert "authorized_calls AS" in detail_call_query
    assert "COUNT(*) OVER" not in detail_run_query
    assert "COUNT(*) OVER" not in detail_call_query
    assert detail_call_query.count("request_json") == 1
    assert "principal_id =" in capture_query
    assert "candidate_requests" not in capture_query
    assert capture_query.count("FROM memory_capture_queue") == 1

    with original_connect(insight_paths.call_log_db_path) as conn:
        conn.execute("ATTACH DATABASE ? AS capture", (str(insight_paths.capture_db_path),))
        conn.execute("ATTACH DATABASE ? AS ome", (str(insight_paths.ome_db_path),))
        query_plan = "\n".join(
            str(row[3]) for row in conn.execute(f"EXPLAIN QUERY PLAN {detail_call_query}")
        )
    for index_name in (
        "provider_call_memcell_id_idx",
        "provider_call_request_id_idx",
        "provider_call_run_id_idx",
        "provider_call_parent_idx",
    ):
        assert f"SEARCH pc USING INDEX {index_name}" in query_plan
    assert "SCAN pc" not in query_plan
