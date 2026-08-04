from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

import pytest

from core.memory.everos_insight import recorder
from core.memory.everos_insight.recorder import (
    ProviderCallInput,
    RecorderHandle,
    initialize_call_log,
    maintain_call_log,
    normalize_provider_call,
)


def _call(**overrides: object) -> ProviderCallInput:
    values: dict[str, object] = {
        "id": "call-1",
        "started_at_ms": 1_700_000_000_000,
        "duration_ms": 125,
        "kind": "llm",
        "stage": "boundary",
        "status": "ok",
        "request": {"messages": [{"role": "user", "content": "hello"}]},
        "response": {"content": "world"},
    }
    values.update(overrides)
    return ProviderCallInput(**values)  # type: ignore[arg-type]


def _private_db_path(tmp_path: Path) -> Path:
    directory = tmp_path / "call-log"
    directory.mkdir(mode=0o700)
    return directory / "call-log.db"


def test_normalize_provider_call_scrubs_every_serialized_column() -> None:
    secret = "sk-super-secret-value"
    unprefixed_secret = "plain-provider-credential"
    local_path = "/Users/alice/private/prompt.txt"
    base_url = "https://provider.example.test/v1"
    row = normalize_provider_call(
        _call(
            model=f"model via {base_url}",
            error=f"Authorization: Bearer token-value at {local_path}",
            request={
                "Authorization": f"Bearer {secret}",
                "x-api-key": unprefixed_secret,
                "Proxy-Authorization": unprefixed_secret,
                "refresh_token": unprefixed_secret,
                "password": unprefixed_secret,
                "session_cookie": unprefixed_secret,
                "signing_private_key": unprefixed_secret,
                "provider_credentials": unprefixed_secret,
                "nested": [f"api_key={secret}", local_path, base_url],
            },
            response={"access_token": secret, "content": f"read file://{local_path}"},
            md_path=local_path,
            request_id=f"request-{secret}",
        ),
        provider_base_urls=(base_url,),
    )

    serialized = json.dumps(asdict(row), sort_keys=True)
    assert secret not in serialized
    assert unprefixed_secret not in serialized
    assert local_path not in serialized
    assert base_url not in serialized
    assert "token-value" not in serialized
    assert "[REDACTED]" in serialized
    assert "[LOCAL_PATH]" in serialized
    assert "[PROVIDER_BASE_URL]" in serialized


def test_free_text_authorization_redacts_basic_and_digest_values() -> None:
    basic = "dXNlcjpwYXNzd29yZA=="
    digest = 'username="alice", realm="private", response="deadbeef"'
    row = normalize_provider_call(
        _call(error=f"Authorization: Basic {basic}\nProxy-Authorization: Digest {digest}\nprovider failed")
    )

    assert row.error is not None
    assert basic not in row.error
    assert digest not in row.error
    assert "Basic" not in row.error
    assert "Digest" not in row.error
    assert row.error.count("[REDACTED]") == 2


def test_llm_capture_has_deterministic_message_and_payload_budgets() -> None:
    messages = [{"role": "user", "content": f"message-{index}-" + ("x" * 17_000)} for index in range(8)]
    row = normalize_provider_call(
        _call(
            request={
                "messages": messages,
                "response_format": {"json_schema": {"name": "Memory", "schema": {"secret": "large"}}},
            },
            response={"content": "r" * 70_000},
        )
    )

    request = json.loads(row.request_json)
    response = json.loads(row.response_json or "null")
    assert len(row.request_json.encode()) <= 64 * 1024
    assert len(row.response_json.encode()) <= 64 * 1024
    assert request["response_format"] == {"name": "Memory"}
    assert request["messages"][0]["role"] == "user"
    assert request["messages"][1] == {"omitted_messages": 6}
    assert request["messages"][-1]["role"] == "user"
    assert request["messages"][0]["content"]["omitted_bytes"] > 8_000
    assert response["content"]["omitted_bytes"] > 0
    assert row.request_bytes > len(row.request_json.encode())
    assert row.response_bytes is not None and row.response_bytes > len(row.response_json.encode())


def test_multimodal_capture_omits_attachments_and_bounds_text_parts() -> None:
    attachment = "data:image/png;base64,raw-attachment-material"
    row = normalize_provider_call(
        _call(
            kind="multimodal_llm",
            request={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "x" * 5_000},
                            {"type": "image_url", "image_url": {"url": attachment}},
                        ],
                    }
                ]
            },
            response={"audio": "raw-audio-material"},
        )
    )

    serialized = (row.request_json, row.response_json or "")
    assert attachment not in serialized[0]
    assert "raw-audio-material" not in serialized[1]
    assert "[ATTACHMENT_OMITTED]" in "".join(serialized)
    assert "omitted_bytes" in serialized[0]


def test_embedding_capture_stores_text_excerpts_and_vector_summary_only() -> None:
    vector_secret = 0.123456789
    inputs = [f"input-{index}-" + ("z" * 3_000) for index in range(20)]
    row = normalize_provider_call(
        _call(
            kind="embedding",
            request={"model": "embed", "dimensions": 3, "input": inputs},
            response={
                "data": [{"embedding": [vector_secret, 0.2, 0.3]} for _ in range(2)],
                "usage": {"prompt_tokens": 20},
            },
        )
    )

    request = json.loads(row.request_json)
    response = json.loads(row.response_json or "null")
    assert request["input_count"] == 20
    assert len(request["inputs"]) == 16
    assert request["omitted_inputs"] == 4
    assert all(len(json.dumps(item, separators=(",", ":")).encode()) <= 2 * 1024 for item in request["inputs"])
    assert response == {"dimension": 3, "usage": {"prompt_tokens": 20}, "vector_count": 2}
    assert str(vector_secret) not in row.response_json
    assert "embedding" not in row.response_json


def test_normalized_row_bounds_large_scalars_usage_and_escaped_text(monkeypatch) -> None:
    huge = "x" * (2 * 1024 * 1024)
    escaped = '\\"\n\t' * (512 * 1024)
    row = normalize_provider_call(
        _call(
            kind="embedding",
            model=huge,
            strategy_name=huge,
            request_id=huge,
            owner_id=huge,
            request={"input": [escaped]},
            response={
                "data": [{"embedding": [0.1, 0.2]}],
                "usage": {"prompt_tokens": 4, "debug": huge},
            },
        )
    )

    request = json.loads(row.request_json)
    response = json.loads(row.response_json or "null")
    assert len(json.dumps(row.model, separators=(",", ":")).encode()) <= 1024
    assert len(json.dumps(row.strategy_name, separators=(",", ":")).encode()) <= 1024
    assert row.request_id is None
    assert row.owner_id is None
    assert len(json.dumps(request["inputs"][0], separators=(",", ":")).encode()) <= 2 * 1024
    assert response["usage"] == {"prompt_tokens": 4}
    encoded_row = json.dumps(asdict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert len(encoded_row.encode()) <= recorder._MAX_ROW_ENCODED_BYTES
    assert huge not in encoded_row

    monkeypatch.setattr(recorder, "_MAX_ROW_ENCODED_BYTES", 1)
    with pytest.raises(ValueError, match="encoded row budget"):
        normalize_provider_call(_call())


def test_call_log_schema_and_database_are_private(tmp_path) -> None:
    db_path = _private_db_path(tmp_path)
    initialize_call_log(db_path)
    initialize_call_log(db_path)

    assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        columns = [row[1] for row in conn.execute("PRAGMA table_info(provider_call)")]
        assert columns == [
            "id",
            "started_at_ms",
            "duration_ms",
            "kind",
            "stage",
            "model",
            "status",
            "error",
            "finish_reason",
            "prompt_tokens",
            "completion_tokens",
            "request_json",
            "response_json",
            "request_bytes",
            "response_bytes",
            "request_id",
            "strategy_name",
            "run_id",
            "attempt",
            "memcell_id",
            "app_id",
            "project_id",
            "owner_id",
            "md_path",
            "entry_id",
            "parent_type",
            "parent_id",
            "dropped_before",
        ]
        indexes = {
            row[1]: tuple(column[2] for column in conn.execute(f"PRAGMA index_info('{row[1]}')"))
            for row in conn.execute("PRAGMA index_list(provider_call)")
            if not row[1].startswith("sqlite_autoindex")
        }
    assert indexes == {
        "provider_call_memcell_id_idx": ("memcell_id",),
        "provider_call_parent_idx": ("parent_type", "parent_id"),
        "provider_call_request_id_idx": ("request_id",),
        "provider_call_run_id_idx": ("run_id",),
        "provider_call_started_at_idx": ("started_at_ms",),
    }


def test_call_log_initializer_rejects_a_future_schema(tmp_path) -> None:
    db_path = _private_db_path(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA user_version = 2")
    db_path.chmod(0o600)

    with pytest.raises(RuntimeError, match="Unsupported call-log schema version: 2"):
        initialize_call_log(db_path)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_scrubbed_row_is_all_that_reaches_sqlite(tmp_path) -> None:
    secret = "sk-database-secret"
    local_path = "/home/alice/private.txt"
    attachment = "raw-attachment-material"
    vector = 0.987654321
    db_path = _private_db_path(tmp_path)
    initialize_call_log(db_path)
    rows = [
        normalize_provider_call(
            _call(
                request={"x-api-key": secret, "messages": [{"content": f"Bearer {secret}"}]},
                response={"content": local_path},
            )
        ),
        normalize_provider_call(
            _call(
                id="call-2",
                kind="multimodal_llm",
                request={"messages": [{"type": "image_url", "image_url": attachment}]},
            )
        ),
        normalize_provider_call(
            _call(
                id="call-3",
                kind="embedding",
                request={"input": ["text"]},
                response={"data": [{"embedding": [vector]}]},
            )
        ),
    ]
    with sqlite3.connect(db_path) as conn:
        for row in rows:
            parameters = asdict(row)
            columns = ", ".join(parameters)
            placeholders = ", ".join(f":{column}" for column in parameters)
            conn.execute(f"INSERT INTO provider_call ({columns}) VALUES ({placeholders})", parameters)
        stored = conn.execute("SELECT * FROM provider_call ORDER BY id").fetchall()

    assert len(stored) == 3
    persisted = b"".join(candidate.read_bytes() for candidate in db_path.parent.glob("call-log.db*"))
    for raw_value in (secret, local_path, attachment, str(vector)):
        assert raw_value not in repr(stored)
        assert raw_value.encode() not in persisted


@pytest.mark.parametrize("payload", [{"bad": object()}, {"bad": float("nan")}, ("tuple",)])
def test_provider_input_rejects_non_json_payloads(payload: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        normalize_provider_call(_call(request=payload))  # type: ignore[arg-type]


def test_call_log_rejects_database_symlink_without_touching_target(tmp_path, monkeypatch) -> None:
    db_path = _private_db_path(tmp_path)
    target = tmp_path / "target.db"
    target.write_bytes(b"sentinel")
    target.chmod(0o600)
    db_path.symlink_to(target)
    monkeypatch.setattr(recorder.sqlite3, "connect", lambda *_args, **_kwargs: pytest.fail("sqlite opened"))

    with pytest.raises(OSError, match="regular file"):
        initialize_call_log(db_path)

    assert target.read_bytes() == b"sentinel"


def test_call_log_rejects_intermediate_symlink_without_creating_database(tmp_path, monkeypatch) -> None:
    target_directory = tmp_path / "target-directory"
    target_directory.mkdir(mode=0o700)
    linked_directory = tmp_path / "linked-directory"
    linked_directory.symlink_to(target_directory, target_is_directory=True)
    db_path = linked_directory / "call-log.db"
    monkeypatch.setattr(recorder.sqlite3, "connect", lambda *_args, **_kwargs: pytest.fail("sqlite opened"))

    with pytest.raises(OSError, match="parent chain"):
        initialize_call_log(db_path)

    assert not (target_directory / "call-log.db").exists()


def test_call_log_rejects_non_absolute_and_missing_parent_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(recorder.sqlite3, "connect", lambda *_args, **_kwargs: pytest.fail("sqlite opened"))
    with pytest.raises(OSError, match="lexical absolute"):
        initialize_call_log(Path("relative/call-log.db"))

    missing_parent = tmp_path / "missing" / "call-log.db"
    with pytest.raises(FileNotFoundError):
        initialize_call_log(missing_parent)
    assert not missing_parent.parent.exists()


def test_call_log_rejects_insecure_directory_and_database_modes(tmp_path) -> None:
    insecure_directory = tmp_path / "insecure"
    insecure_directory.mkdir(mode=0o755)
    with pytest.raises(OSError, match="mode 0700"):
        initialize_call_log(insecure_directory / "call-log.db")
    assert not (insecure_directory / "call-log.db").exists()

    db_path = _private_db_path(tmp_path)
    db_path.write_bytes(b"sentinel")
    db_path.chmod(0o644)
    with pytest.raises(OSError, match="mode 0600"):
        initialize_call_log(db_path)
    assert db_path.read_bytes() == b"sentinel"


def test_call_log_rejects_unowned_directory_before_open(tmp_path, monkeypatch) -> None:
    db_path = _private_db_path(tmp_path)
    current_uid = os.getuid()
    monkeypatch.setattr(recorder.os, "getuid", lambda: current_uid + 1)

    with pytest.raises(OSError, match="owned by the current user"):
        initialize_call_log(db_path)

    assert not db_path.exists()


async def test_recorder_batches_and_reports_oldest_queue_drops(tmp_path, monkeypatch) -> None:
    db_path = _private_db_path(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    batch_sizes: list[int] = []
    original_connection = recorder._database_connection
    original_write_batch = recorder._write_batch

    @contextmanager
    def gated_connection(path):
        entered.set()
        assert release.wait(2)
        with original_connection(path) as conn:
            yield conn

    def observed_write_batch(conn, rows, should_abort):
        batch_sizes.append(len(rows))
        return original_write_batch(conn, rows, should_abort)

    monkeypatch.setattr(recorder, "_database_connection", gated_connection)
    monkeypatch.setattr(recorder, "_write_batch", observed_write_batch)
    handle = RecorderHandle(db_path)
    handle.start()
    assert entered.wait(2)

    started_at_ms = int(time.time() * 1000)
    for index in range(300):
        handle.submit(_call(id=f"call-{index}", started_at_ms=started_at_ms + index))
    release.set()
    await handle.close()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT started_at_ms, dropped_before FROM provider_call ORDER BY started_at_ms").fetchall()
    assert len(rows) == 256
    assert rows[0] == (started_at_ms + 44, 44)
    assert rows[-1] == (started_at_ms + 299, 0)
    assert batch_sizes
    assert max(batch_sizes) <= recorder._WRITE_BATCH_SIZE
    assert len(batch_sizes) < len(rows)


async def test_recorder_swallows_bad_inputs_and_persists_the_next_call(tmp_path) -> None:
    db_path = _private_db_path(tmp_path)
    handle = RecorderHandle(db_path)
    handle.start()
    handle.submit(_call(id="invalid", request={"bad": object()}))  # type: ignore[arg-type]
    handle.submit(_call(id="valid", started_at_ms=int(time.time() * 1000)))
    await handle.close()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT id, dropped_before FROM provider_call").fetchall()
    assert rows == [("valid", 1)]


async def test_recorder_disables_after_twenty_consecutive_writer_failures(tmp_path, monkeypatch) -> None:
    db_path = _private_db_path(tmp_path)
    failed = threading.Event()
    attempts = 0

    def failing_write_batch(_conn, _rows, _should_abort):
        nonlocal attempts
        attempts += 1
        if attempts == recorder._MAX_CONSECUTIVE_WRITER_FAILURES:
            failed.set()
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(recorder, "_write_batch", failing_write_batch)
    handle = RecorderHandle(db_path)
    handle.start()
    handle.submit(_call())
    assert failed.wait(2)

    assert attempts == recorder._MAX_CONSECUTIVE_WRITER_FAILURES
    assert handle.health == {"state": "disabled", "reason": "writer_failures"}
    await handle.close()


async def test_recorder_resets_writer_failure_health_after_success(tmp_path, monkeypatch) -> None:
    db_path = _private_db_path(tmp_path)
    succeeded = threading.Event()
    attempts = 0
    original_write_batch = recorder._write_batch

    def transient_write_batch(conn, rows, should_abort):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("locked")
        result = original_write_batch(conn, rows, should_abort)
        succeeded.set()
        return result

    monkeypatch.setattr(recorder, "_write_batch", transient_write_batch)
    handle = RecorderHandle(db_path)
    handle.start()
    handle.submit(_call())
    assert succeeded.wait(2)
    assert handle.health == {"state": "active", "reason": None}
    await handle.close()


async def test_close_deadline_rolls_back_and_connection_closes_on_writer_thread(tmp_path, monkeypatch) -> None:
    db_path = _private_db_path(tmp_path)
    insert_started = threading.Event()
    release_insert = threading.Event()
    created_threads: list[int] = []
    closed_threads: list[int] = []
    real_connect = sqlite3.connect

    class BlockingConnection(sqlite3.Connection):
        def executemany(self, sql, parameters):
            result = super().executemany(sql, parameters)
            insert_started.set()
            assert release_insert.wait(2)
            return result

        def close(self):
            closed_threads.append(threading.get_ident())
            return super().close()

    def tracking_connect(*args, **kwargs):
        created_threads.append(threading.get_ident())
        return real_connect(*args, **kwargs, factory=BlockingConnection)

    monkeypatch.setattr(recorder.sqlite3, "connect", tracking_connect)
    handle = RecorderHandle(db_path)
    handle.start()
    handle.submit(_call())
    assert insert_started.wait(2)

    close_task = asyncio.create_task(handle.close(timeout=0.0))
    assert await asyncio.to_thread(handle._close_requested.wait, 2)
    release_insert.set()
    await close_task

    assert created_threads == closed_threads
    assert len(created_threads) == 1
    assert created_threads[0] != threading.get_ident()
    with real_connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM provider_call").fetchone()[0] == 0


def test_host_maintenance_prunes_age_and_count(tmp_path) -> None:
    db_path = _private_db_path(tmp_path)
    initialize_call_log(db_path)
    now_ms = 2_000_000_000_000
    cutoff_ms = now_ms - recorder._RETENTION_AGE_MS
    calls = [
        normalize_provider_call(_call(id=f"recent-{index}", started_at_ms=now_ms - index)) for index in range(5_005)
    ]
    calls.extend(
        normalize_provider_call(_call(id=f"old-{index}", started_at_ms=cutoff_ms - index - 1)) for index in range(5)
    )
    parameters = [asdict(call) for call in calls]
    columns = ", ".join(parameters[0])
    placeholders = ", ".join(f":{column}" for column in parameters[0])
    with sqlite3.connect(db_path) as conn:
        conn.executemany(f"INSERT INTO provider_call ({columns}) VALUES ({placeholders})", parameters)

    assert maintain_call_log(db_path, now_ms=now_ms) is None
    with sqlite3.connect(db_path) as conn:
        count, oldest = conn.execute("SELECT count(*), min(started_at_ms) FROM provider_call").fetchone()
    assert count == 5_000
    assert oldest >= cutoff_ms


def test_host_maintenance_bounds_checkpoint_and_vacuum_passes(tmp_path, monkeypatch) -> None:
    db_path = _private_db_path(tmp_path)
    initialize_call_log(db_path)
    statements: list[str] = []
    size_reads = 0
    original_connection = recorder._database_connection

    @contextmanager
    def traced_connection(path):
        with original_connection(path) as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    def decreasing_oversize(_path):
        nonlocal size_reads
        size_reads += 1
        return recorder._SOFT_STORAGE_BYTES + 100 - size_reads

    monkeypatch.setattr(recorder, "_database_connection", traced_connection)
    monkeypatch.setattr(recorder, "_call_log_size_bytes", decreasing_oversize)
    assert maintain_call_log(db_path) is None

    vacuum = [statement for statement in statements if "incremental_vacuum" in statement]
    checkpoints = [statement for statement in statements if "wal_checkpoint" in statement]
    assert len(vacuum) == recorder._MAX_VACUUM_PASSES
    assert len(checkpoints) == recorder._MAX_VACUUM_PASSES + 1
    assert size_reads == recorder._MAX_VACUUM_PASSES + 1


async def test_corrupt_call_log_stays_degraded_and_is_never_recreated(tmp_path) -> None:
    db_path = _private_db_path(tmp_path)
    corrupt_bytes = b"not a sqlite database"
    db_path.write_bytes(corrupt_bytes)
    db_path.chmod(0o600)

    assert maintain_call_log(db_path) == "call_log_corrupt"
    assert maintain_call_log(db_path) == "call_log_corrupt"
    assert db_path.read_bytes() == corrupt_bytes

    handle = RecorderHandle(db_path)
    handle.start()
    handle.submit(_call())
    await handle.close()
    assert handle.health == {"state": "degraded", "reason": "call_log_corrupt"}
    assert db_path.read_bytes() == corrupt_bytes
    assert sorted(path.name for path in db_path.parent.iterdir()) == [db_path.name]
