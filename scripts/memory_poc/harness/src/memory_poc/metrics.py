from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import ensure_regular_file_mode


@dataclass(frozen=True)
class CallMetrics:
    llm_calls: int = 0
    embedding_calls: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    embedding_input_tokens: int = 0
    llm_usage_records: int = 0
    embedding_usage_records: int = 0
    ingestion_llm_calls: int = 0
    ingestion_embedding_calls: int = 0
    ingestion_llm_input_tokens: int = 0
    ingestion_llm_output_tokens: int = 0
    ingestion_embedding_input_tokens: int = 0
    ingestion_llm_usage_records: int = 0
    ingestion_embedding_usage_records: int = 0


def classify_request_path(path: str) -> str:
    return "embedding" if "embeddings" in path.lower() else "llm"


def append_request_metric(
    path: Path,
    *,
    kind: str,
    usage: dict[str, Any] | None = None,
    phase: str = "unattributed",
) -> None:
    """Persist only request category and token counters, never URL/body/header data."""
    record = {"kind": kind, "phase": phase if phase in {"ingestion", "read", "health"} else "unattributed"}
    if isinstance(usage, dict):
        for source, target in (("prompt_tokens", "input_tokens"), ("input_tokens", "input_tokens"), ("completion_tokens", "output_tokens"), ("output_tokens", "output_tokens")):
            value = usage.get(source)
            if isinstance(value, int) and value >= 0:
                record[target] = value
    encoded = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)
    ensure_regular_file_mode(path)


def read_call_metrics(path: Path) -> CallMetrics:
    if not path.is_file():
        return CallMetrics()
    counts = {
        "llm_calls": 0,
        "embedding_calls": 0,
        "llm_input_tokens": 0,
        "llm_output_tokens": 0,
        "embedding_input_tokens": 0,
        "llm_usage_records": 0,
        "embedding_usage_records": 0,
        "ingestion_llm_calls": 0,
        "ingestion_embedding_calls": 0,
        "ingestion_llm_input_tokens": 0,
        "ingestion_llm_output_tokens": 0,
        "ingestion_embedding_input_tokens": 0,
        "ingestion_llm_usage_records": 0,
        "ingestion_embedding_usage_records": 0,
    }
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        ingestion = item.get("phase") == "ingestion"
        input_tokens = item.get("input_tokens", 0)
        output_tokens = item.get("output_tokens", 0)
        usage_observed = "input_tokens" in item or "output_tokens" in item
        if kind == "embedding":
            counts["embedding_calls"] += 1
            if ingestion:
                counts["ingestion_embedding_calls"] += 1
            if isinstance(input_tokens, int) and input_tokens >= 0:
                counts["embedding_input_tokens"] += input_tokens
                if ingestion:
                    counts["ingestion_embedding_input_tokens"] += input_tokens
            if usage_observed:
                counts["embedding_usage_records"] += 1
                if ingestion:
                    counts["ingestion_embedding_usage_records"] += 1
        elif kind == "llm":
            counts["llm_calls"] += 1
            if ingestion:
                counts["ingestion_llm_calls"] += 1
            if isinstance(input_tokens, int) and input_tokens >= 0:
                counts["llm_input_tokens"] += input_tokens
                if ingestion:
                    counts["ingestion_llm_input_tokens"] += input_tokens
            if isinstance(output_tokens, int) and output_tokens >= 0:
                counts["llm_output_tokens"] += output_tokens
                if ingestion:
                    counts["ingestion_llm_output_tokens"] += output_tokens
            if usage_observed:
                counts["llm_usage_records"] += 1
                if ingestion:
                    counts["ingestion_llm_usage_records"] += 1
    return CallMetrics(**counts)
