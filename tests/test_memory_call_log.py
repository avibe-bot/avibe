from __future__ import annotations

import json
import sqlite3
import stat
from dataclasses import asdict

import pytest

from core.memory.everos_insight.recorder import (
    ProviderCallInput,
    initialize_call_log,
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


def test_call_log_schema_and_database_are_private(tmp_path) -> None:
    db_path = tmp_path / "call-log" / "call-log.db"
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
    db_path = tmp_path / "call-log" / "call-log.db"
    db_path.parent.mkdir()
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA user_version = 2")

    with pytest.raises(RuntimeError, match="Unsupported call-log schema version: 2"):
        initialize_call_log(db_path)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_scrubbed_row_is_all_that_reaches_sqlite(tmp_path) -> None:
    secret = "sk-database-secret"
    local_path = "/home/alice/private.txt"
    attachment = "raw-attachment-material"
    vector = 0.987654321
    db_path = tmp_path / "call-log" / "call-log.db"
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
