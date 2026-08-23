"""Focused diagnostics tests for independent best-effort sources."""

import json
from pathlib import Path
import sqlite3

import pytest

from core.memory.everos_insight.recorder import initialize_call_log
from core.memory.everos_insight.reader import MemoryInsightPaths, MemoryInsightReader


PRINCIPAL = "u-" + "a" * 32
OTHER_PRINCIPAL = "u-" + "b" * 32


def _reader(tmp_path: Path) -> MemoryInsightReader:
    return MemoryInsightReader(
        MemoryInsightPaths(
            everos_root=tmp_path / "everos",
            capture_db_path=tmp_path / "memory.sqlite",
            call_log_db_path=tmp_path / "calls.sqlite",
        )
    )


def test_capture_diagnostics_are_unavailable_without_delivery_history(tmp_path: Path) -> None:
    reader = _reader(tmp_path)
    result = reader.list_entries((PRINCIPAL, "default"), None, 10)
    assert result["entries"] == []
    assert result["sections"]["capture"]["status"] == "unavailable"
    assert result["sections"]["everos"] == {
        "status": "unavailable",
        "observed_at": result["sections"]["everos"]["observed_at"],
        "reason": "provider_memory_unavailable",
    }


def test_scoped_diagnostics_validate_identity_before_reading(tmp_path: Path) -> None:
    reader = _reader(tmp_path)
    with pytest.raises(ValueError):
        reader.list_unlinked_calls(("not-a-principal", "default"), 1)


def test_processing_source_observation_keeps_calls_independent(tmp_path: Path) -> None:
    observation = _reader(tmp_path).source_observation()
    assert observation.capture.status == "unavailable"
    assert observation.calls.status == "unavailable"


def test_empty_authorized_call_log_is_available_not_unavailable(tmp_path: Path) -> None:
    reader = _reader(tmp_path)
    initialize_call_log(tmp_path / "calls.sqlite")

    result = reader.list_unlinked_calls((PRINCIPAL, "default"), 20)

    assert result["calls"] == []
    assert result["truncated"] is False
    assert result["sections"]["calls"]["status"] == "available"
    assert reader.source_observation().calls.status == "available"


def test_empty_call_log_under_uri_significant_home_is_available(tmp_path: Path) -> None:
    home = tmp_path / "home #1"
    home.mkdir(mode=0o700)
    call_log = home / "calls.sqlite"
    initialize_call_log(call_log)
    reader = MemoryInsightReader(
        MemoryInsightPaths(
            everos_root=home / "everos",
            capture_db_path=home / "memory.sqlite",
            call_log_db_path=call_log,
        )
    )

    result = reader.list_unlinked_calls((PRINCIPAL, "default"), 20)

    assert result["calls"] == []
    assert result["sections"]["calls"]["status"] == "available"


def test_provider_calls_are_scoped_by_direct_authorization_evidence(tmp_path: Path) -> None:
    call_log = tmp_path / "calls.sqlite"
    initialize_call_log(call_log)

    def insert_call(call_id: str, owner_id: str | None, project_id: str | None) -> None:
        with sqlite3.connect(call_log) as conn:
            conn.execute(
                """
                INSERT INTO provider_call (
                    id, started_at_ms, duration_ms, kind, stage, status,
                    request_json, request_bytes, project_id, owner_id
                ) VALUES (?, ?, 1, 'llm', 'boundary', 'ok', ?, 2, ?, ?)
                """,
                (
                    call_id,
                    len(call_id),
                    json.dumps({"call": call_id}),
                    project_id,
                    owner_id,
                ),
            )

    insert_call("alice", PRINCIPAL, "default")
    insert_call("assistant", f"{PRINCIPAL}-agent", "default")
    insert_call("bob", OTHER_PRINCIPAL, "default")
    insert_call("unscoped", None, None)
    reader = _reader(tmp_path)

    scoped = reader.list_unlinked_calls((PRINCIPAL, "default"), 20)
    admin = reader.list_admin_unlinked_calls(20)

    assert {call["id"] for call in scoped["calls"]} == {"alice", "assistant"}
    assert {call["principal_id"] for call in scoped["calls"]} == {PRINCIPAL}
    assert {call["id"] for call in admin["calls"]} == {
        "alice",
        "assistant",
        "bob",
    }
    assert "unscoped" not in {call["id"] for call in admin["calls"]}


def test_memory_entries_and_linked_calls_remain_authorization_scoped(
    tmp_path: Path,
) -> None:
    reader = _reader(tmp_path)
    system_db = tmp_path / "everos" / ".index" / "sqlite" / "system.db"
    system_db.parent.mkdir(parents=True)
    with sqlite3.connect(system_db) as conn:
        conn.execute(
            """
            CREATE TABLE memcell (
                memcell_id TEXT PRIMARY KEY,
                app_id TEXT,
                project_id TEXT,
                message_ids_json TEXT,
                sender_ids_json TEXT,
                payload_json TEXT,
                timestamp INTEGER
            )
            """
        )

        def insert_memcell(
            memcell_id: str, owner_id: str, timestamp: int
        ) -> None:
            conn.execute(
                "INSERT INTO memcell VALUES (?, 'avibe', 'default', ?, ?, ?, ?)",
                (
                    memcell_id,
                    json.dumps([f"message-{memcell_id}"]),
                    json.dumps([owner_id]),
                    json.dumps(
                        {
                            "items": [
                                {
                                    "role": "user",
                                    "sender_id": owner_id,
                                    "content": f"preview-{memcell_id}",
                                }
                            ]
                        }
                    ),
                    timestamp,
                ),
            )

        insert_memcell("mc-alice", PRINCIPAL, 3)
        insert_memcell("mc-assistant", f"{PRINCIPAL}-agent", 2)
        insert_memcell("mc-bob", OTHER_PRINCIPAL, 1)
        insert_memcell("mc-invalid", "not-an-owner", 4)

    call_log = tmp_path / "calls.sqlite"
    initialize_call_log(call_log)
    with sqlite3.connect(call_log) as conn:
        conn.executemany(
            """
            INSERT INTO provider_call (
                id, started_at_ms, duration_ms, kind, stage, status,
                request_json, request_bytes, memcell_id, project_id, owner_id
            ) VALUES (?, 1, 1, 'llm', 'cascade', 'ok', '{}', 2, ?, 'default', ?)
            """,
            [
                ("call-alice", "mc-alice", PRINCIPAL),
                ("call-cross-owner", "mc-alice", OTHER_PRINCIPAL),
                ("call-assistant", "mc-assistant", f"{PRINCIPAL}-agent"),
            ],
        )

    first = reader.list_entries((PRINCIPAL, "default"), None, 1)
    second = reader.list_entries(
        (PRINCIPAL, "default"), first["next_cursor"], 1
    )
    admin = reader.list_admin_entries(None, 10)
    detail = reader.entry_detail((PRINCIPAL, "default"), "mc-alice")

    assert [entry["memcell_id"] for entry in first["entries"]] == ["mc-alice"]
    assert [entry["memcell_id"] for entry in second["entries"]] == [
        "mc-assistant"
    ]
    assert first["entries"][0]["authorized_call_count"] == 1
    assert first["sections"]["capture"]["status"] == "unavailable"
    assert first["sections"]["everos"]["status"] == "available"
    assert {entry["memcell_id"] for entry in admin["entries"]} == {
        "mc-alice",
        "mc-assistant",
        "mc-bob",
    }
    assert detail["status"] == "ok"
    assert [call["id"] for call in detail["calls"]] == ["call-alice"]
    assert detail["capture"] == {
        "status": "unavailable",
        "reason": "volatile_delivery_state",
    }
    assert (
        reader.entry_detail((OTHER_PRINCIPAL, "default"), "mc-alice")[
            "status"
        ]
        == "not_found"
    )
