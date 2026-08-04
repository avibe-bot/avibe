from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

JsonPrimitive: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]
ProviderKind: TypeAlias = Literal["llm", "multimodal_llm", "embedding"]

_REDACTED = "[REDACTED]"
_ATTACHMENT_OMITTED = "[ATTACHMENT_OMITTED]"
_LOCAL_PATH = "[LOCAL_PATH]"
_PROVIDER_BASE_URL = "[PROVIDER_BASE_URL]"
_LLM_MESSAGE_BYTES = 16 * 1024
_LLM_PAYLOAD_BYTES = 64 * 1024
_MULTIMODAL_STRING_BYTES = 4 * 1024
_EMBEDDING_INPUT_BYTES = 2 * 1024
_EMBEDDING_INPUT_COUNT = 16
_ERROR_BYTES = 4 * 1024

_SECRET_KEY_SUFFIXES = (
    "accesstoken",
    "apikey",
    "authorization",
    "authtoken",
    "clientsecret",
    "providertoken",
    "refreshtoken",
    "secretkey",
)
_ATTACHMENT_KEYS = frozenset(
    {
        "attachment",
        "attachments",
        "audio",
        "b64json",
        "bytes",
        "file",
        "filedata",
        "image",
        "imageurl",
    }
)
_ATTACHMENT_PART_TYPES = frozenset(
    {
        "attachment",
        "audio",
        "document",
        "file",
        "image",
        "image_url",
        "input_audio",
        "input_file",
        "input_image",
    }
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_LABELED_SECRET_RE = re.compile(
    r"(?i)(\b(?:api[-_ ]?key|authorization|access[-_ ]?token|auth[-_ ]?token)\s*[:=]\s*)"
    r"([^\s,;]+)"
)
_PREFIXED_KEY_RE = re.compile(r"(?<![A-Za-z0-9])(?:sk|rk|pk|api)-[A-Za-z0-9_-]{8,}")
_FILE_URL_RE = re.compile(r"(?i)\bfile:///(?:[^\s\"'<>]|\\ )+")
_POSIX_PATH_RE = re.compile(r"(?<![:/\w])/(?:[^\s\"'<>]|\\ )+")
_WINDOWS_PATH_RE = re.compile(r"(?<!\w)(?:[A-Za-z]:\\|\\\\)[^\s\"'<>]+")


@dataclass(frozen=True, slots=True)
class ProviderCallInput:
    id: str
    started_at_ms: int
    duration_ms: int
    kind: ProviderKind
    stage: str
    status: str
    request: JsonValue
    response: JsonValue | None = None
    model: str | None = None
    error: str | None = None
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    request_id: str | None = None
    strategy_name: str | None = None
    run_id: str | None = None
    attempt: int | None = None
    memcell_id: str | None = None
    app_id: str | None = None
    project_id: str | None = None
    owner_id: str | None = None
    md_path: str | None = None
    entry_id: str | None = None
    parent_type: str | None = None
    parent_id: str | None = None
    dropped_before: int = 0


@dataclass(frozen=True, slots=True)
class ProviderCallRow:
    id: str
    started_at_ms: int
    duration_ms: int
    kind: str
    stage: str
    model: str | None
    status: str
    error: str | None
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    request_json: str
    response_json: str | None
    request_bytes: int
    response_bytes: int | None
    request_id: str | None
    strategy_name: str | None
    run_id: str | None
    attempt: int | None
    memcell_id: str | None
    app_id: str | None
    project_id: str | None
    owner_id: str | None
    md_path: str | None
    entry_id: str | None
    parent_type: str | None
    parent_id: str | None
    dropped_before: int


def normalize_provider_call(
    call: ProviderCallInput,
    *,
    provider_base_urls: Sequence[str] = (),
) -> ProviderCallRow:
    """Turn primitive provider data into one bounded, storage-ready row."""

    _validate_call(call)
    base_urls = tuple(url.rstrip("/") for url in provider_base_urls if url)
    request = _scrub_json(call.request, base_urls=base_urls)
    response = _scrub_json(call.response, base_urls=base_urls) if call.response is not None else None
    request_bytes = _json_size(request)
    response_bytes = _json_size(response) if response is not None else None

    if call.kind == "embedding":
        request = _embedding_request(request)
        response = _embedding_response(response)
    else:
        if call.kind == "multimodal_llm":
            request = _sanitize_multimodal(request)
            response = _sanitize_multimodal(response)
        request = _llm_request(request)
        response = _bounded_json(response, _LLM_PAYLOAD_BYTES) if response is not None else None

    scrub = lambda value: _scrub_optional_text(value, base_urls=base_urls)
    return ProviderCallRow(
        id=scrub(call.id) or "",
        started_at_ms=call.started_at_ms,
        duration_ms=call.duration_ms,
        kind=call.kind,
        stage=scrub(call.stage) or "",
        model=scrub(call.model),
        status=scrub(call.status) or "",
        error=_bounded_text(scrub(call.error), _ERROR_BYTES),
        finish_reason=scrub(call.finish_reason),
        prompt_tokens=call.prompt_tokens,
        completion_tokens=call.completion_tokens,
        request_json=_encode_json(request),
        response_json=_encode_json(response) if response is not None else None,
        request_bytes=request_bytes,
        response_bytes=response_bytes,
        request_id=scrub(call.request_id),
        strategy_name=scrub(call.strategy_name),
        run_id=scrub(call.run_id),
        attempt=call.attempt,
        memcell_id=scrub(call.memcell_id),
        app_id=scrub(call.app_id),
        project_id=scrub(call.project_id),
        owner_id=scrub(call.owner_id),
        md_path=scrub(call.md_path),
        entry_id=scrub(call.entry_id),
        parent_type=scrub(call.parent_type),
        parent_id=scrub(call.parent_id),
        dropped_before=call.dropped_before,
    )


def initialize_call_log(db_path: Path) -> None:
    """Initialize the v1 call-log schema without retaining a connection."""

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _enforce_private_directory(db_path.parent)
    with _database_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS provider_call (
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
            ) STRICT;
            CREATE INDEX IF NOT EXISTS provider_call_request_id_idx
                ON provider_call(request_id);
            CREATE INDEX IF NOT EXISTS provider_call_run_id_idx
                ON provider_call(run_id);
            CREATE INDEX IF NOT EXISTS provider_call_memcell_id_idx
                ON provider_call(memcell_id);
            CREATE INDEX IF NOT EXISTS provider_call_started_at_idx
                ON provider_call(started_at_ms DESC);
            CREATE INDEX IF NOT EXISTS provider_call_parent_idx
                ON provider_call(parent_type, parent_id);
            PRAGMA user_version = 1;
            """
        )


@contextmanager
def _database_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path, timeout=1.0, isolation_level=None)
    try:
        _enforce_private_database_files(db_path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1}:
            raise RuntimeError(f"Unsupported call-log schema version: {version}")
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.execute("PRAGMA journal_mode = WAL")
        _enforce_private_database_files(db_path)
        conn.execute("PRAGMA busy_timeout = 1000")
        yield conn
    finally:
        conn.close()
        _enforce_private_database_files(db_path)


def _validate_call(call: ProviderCallInput) -> None:
    for name in ("id", "stage", "status"):
        value = getattr(call, name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty string")
    if call.kind not in {"llm", "multimodal_llm", "embedding"}:
        raise ValueError("unsupported provider call kind")
    for name in ("started_at_ms", "duration_ms", "dropped_before"):
        value = getattr(call, name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    for name in ("prompt_tokens", "completion_tokens", "attempt"):
        value = getattr(call, name)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise ValueError(f"{name} must be a non-negative integer or None")
    for name in (
        "model",
        "error",
        "finish_reason",
        "request_id",
        "strategy_name",
        "run_id",
        "memcell_id",
        "app_id",
        "project_id",
        "owner_id",
        "md_path",
        "entry_id",
        "parent_type",
        "parent_id",
    ):
        value = getattr(call, name)
        if value is not None and not isinstance(value, str):
            raise TypeError(f"{name} must be a string or None")
    _validate_json(call.request)
    if call.response is not None:
        _validate_json(call.response)


def _validate_json(value: JsonValue, *, depth: int = 0) -> None:
    if depth > 64:
        raise ValueError("provider JSON exceeds maximum nesting depth")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("provider JSON contains a non-finite number")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item, depth=depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _validate_json(item, depth=depth + 1)
        return
    raise TypeError("provider payload must contain only JSON-compatible primitives")


def _scrub_json(value: JsonValue, *, base_urls: tuple[str, ...]) -> JsonValue:
    if isinstance(value, str):
        return _scrub_text(value, base_urls=base_urls)
    if isinstance(value, list):
        return [_scrub_json(item, base_urls=base_urls) for item in value]
    if isinstance(value, dict):
        scrubbed: dict[str, JsonValue] = {}
        for key, item in value.items():
            clean_key = _scrub_text(key, base_urls=base_urls)
            if _is_secret_key(key):
                scrubbed[clean_key] = _REDACTED
            else:
                scrubbed[clean_key] = _scrub_json(item, base_urls=base_urls)
        return scrubbed
    return value


def _scrub_optional_text(value: str | None, *, base_urls: tuple[str, ...]) -> str | None:
    return _scrub_text(value, base_urls=base_urls) if value is not None else None


def _scrub_text(value: str, *, base_urls: tuple[str, ...]) -> str:
    scrubbed = value
    for base_url in sorted(base_urls, key=len, reverse=True):
        scrubbed = scrubbed.replace(base_url, _PROVIDER_BASE_URL)
    scrubbed = _BEARER_RE.sub("Bearer " + _REDACTED, scrubbed)
    scrubbed = _LABELED_SECRET_RE.sub(lambda match: match.group(1) + _REDACTED, scrubbed)
    scrubbed = _PREFIXED_KEY_RE.sub(_REDACTED, scrubbed)
    scrubbed = _FILE_URL_RE.sub(_LOCAL_PATH, scrubbed)
    scrubbed = _WINDOWS_PATH_RE.sub(_LOCAL_PATH, scrubbed)
    return _POSIX_PATH_RE.sub(_LOCAL_PATH, scrubbed)


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _is_secret_key(value: str) -> bool:
    return _normalized_key(value).endswith(_SECRET_KEY_SUFFIXES)


def _llm_request(value: JsonValue) -> JsonValue:
    if not isinstance(value, dict):
        return _bounded_json(value, _LLM_PAYLOAD_BYTES)
    result = dict(value)
    messages = result.get("messages")
    if isinstance(messages, list):
        bounded_messages = [_bounded_json(message, _LLM_MESSAGE_BYTES) for message in messages]
        result["messages"] = bounded_messages
    response_format = result.get("response_format")
    if isinstance(response_format, dict):
        result["response_format"] = _response_schema_name(response_format)
    if _json_size(result) <= _LLM_PAYLOAD_BYTES:
        return result
    if isinstance(messages, list) and len(messages) > 2:
        result["messages"] = [
            _bounded_json(messages[0], _LLM_MESSAGE_BYTES),
            {"omitted_messages": len(messages) - 2},
            _bounded_json(messages[-1], _LLM_MESSAGE_BYTES),
        ]
    return _bounded_json(result, _LLM_PAYLOAD_BYTES)


def _response_schema_name(response_format: dict[str, JsonValue]) -> JsonValue:
    name = response_format.get("name")
    if isinstance(name, str):
        return {"name": name}
    schema = response_format.get("json_schema")
    if isinstance(schema, dict) and isinstance(schema.get("name"), str):
        return {"name": schema["name"]}
    return {"name": None}


def _sanitize_multimodal(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return _bounded_json(value, _MULTIMODAL_STRING_BYTES)
    if isinstance(value, list):
        return [_sanitize_multimodal(item) for item in value]
    if not isinstance(value, dict):
        return value
    part_type = value.get("type")
    if isinstance(part_type, str) and part_type.casefold() in _ATTACHMENT_PART_TYPES:
        return {"type": part_type, "attachment": _ATTACHMENT_OMITTED}
    result: dict[str, JsonValue] = {}
    for key, item in value.items():
        if _normalized_key(key) in _ATTACHMENT_KEYS:
            result[key] = _ATTACHMENT_OMITTED
        else:
            result[key] = _sanitize_multimodal(item)
    return result


def _embedding_request(value: JsonValue) -> JsonValue:
    source = value if isinstance(value, dict) else {"input": value}
    raw_inputs = source.get("input", source.get("inputs", []))
    inputs = raw_inputs if isinstance(raw_inputs, list) else [raw_inputs]
    excerpts: list[JsonValue] = []
    for item in inputs[:_EMBEDDING_INPUT_COUNT]:
        if isinstance(item, str):
            excerpts.append(_excerpt(item, _EMBEDDING_INPUT_BYTES))
        else:
            excerpts.append({"omitted_input": True})
    result: dict[str, JsonValue] = {
        "model": source.get("model") if isinstance(source.get("model"), str) else None,
        "dimensions": source.get("dimensions") if isinstance(source.get("dimensions"), int) else None,
        "input_count": len(inputs),
        "inputs": excerpts,
    }
    if len(inputs) > _EMBEDDING_INPUT_COUNT:
        result["omitted_inputs"] = len(inputs) - _EMBEDDING_INPUT_COUNT
    return result


def _embedding_response(value: JsonValue | None) -> JsonValue | None:
    if value is None:
        return None
    source = value if isinstance(value, dict) else {}
    vectors = source.get("vectors", source.get("data", []))
    vector_count = len(vectors) if isinstance(vectors, list) else 0
    dimension = source.get("dimension")
    if not isinstance(dimension, int) and isinstance(vectors, list) and vectors:
        first = vectors[0]
        if isinstance(first, list):
            dimension = len(first)
        elif isinstance(first, dict) and isinstance(first.get("embedding"), list):
            dimension = len(first["embedding"])
    usage = source.get("usage")
    return {
        "vector_count": vector_count,
        "dimension": dimension if isinstance(dimension, int) else None,
        "usage": usage if isinstance(usage, dict) else None,
    }


def _bounded_json(value: JsonValue, limit: int) -> JsonValue:
    size = _json_size(value)
    if size <= limit:
        return value
    if isinstance(value, str):
        return _excerpt(value, limit)
    if isinstance(value, dict) and value:
        per_value_limit = max(64, (limit - _json_size({key: None for key in value})) // len(value))
        bounded = {key: _bounded_json(item, per_value_limit) for key, item in value.items()}
        if _json_size(bounded) <= limit:
            return bounded
    if isinstance(value, list) and value:
        if len(value) == 1:
            bounded_list = [_bounded_json(value[0], max(64, limit - 2))]
        else:
            middle: list[JsonValue] = [{"omitted_items": len(value) - 2}] if len(value) > 2 else []
            middle_bytes = sum(_json_size(item) + 1 for item in middle)
            item_limit = max(64, (limit - middle_bytes - 3) // 2)
            bounded_list = [
                _bounded_json(value[0], item_limit),
                *middle,
                _bounded_json(value[-1], item_limit),
            ]
        if _json_size(bounded_list) <= limit:
            return bounded_list
    return {"omitted_bytes": size}


def _excerpt(value: str, limit: int) -> JsonValue:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    marker_overhead = len(_encode_json({"excerpt": "", "omitted_bytes": len(encoded)}).encode("utf-8"))
    excerpt_limit = max(0, limit - marker_overhead)
    excerpt = encoded[:excerpt_limit].decode("utf-8", errors="ignore")
    omitted = len(encoded) - len(excerpt.encode("utf-8"))
    return {"excerpt": excerpt, "omitted_bytes": omitted}


def _bounded_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    suffix = f" [omitted_bytes={len(encoded)}]"
    prefix_limit = max(0, limit - len(suffix.encode("utf-8")))
    prefix = encoded[:prefix_limit].decode("utf-8", errors="ignore")
    omitted = len(encoded) - len(prefix.encode("utf-8"))
    return f"{prefix} [omitted_bytes={omitted}]"


def _json_size(value: JsonValue) -> int:
    return len(_encode_json(value).encode("utf-8"))


def _encode_json(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _enforce_private_directory(directory: Path) -> None:
    info = os.lstat(directory)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError("Call-log directory must be a directory")
    os.chmod(directory, 0o700)
    if stat.S_IMODE(os.lstat(directory).st_mode) != 0o700:
        raise OSError("Call-log directory is not owner-only")


def _enforce_private_database_files(db_path: Path) -> None:
    for candidate in (
        db_path,
        db_path.with_name(f"{db_path.name}-wal"),
        db_path.with_name(f"{db_path.name}-shm"),
    ):
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise OSError("Call-log database path must be a regular file")
        os.chmod(candidate, 0o600)
        if stat.S_IMODE(os.lstat(candidate).st_mode) != 0o600:
            raise OSError("Call-log database is not owner-only")
