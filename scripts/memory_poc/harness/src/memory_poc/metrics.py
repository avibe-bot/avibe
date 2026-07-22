from __future__ import annotations

import json
import os
import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import ensure_regular_file_mode

_HOSTNAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


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


@dataclass(frozen=True)
class EgressObservation:
    """Redacted child-network evidence for the final hostname-only report."""

    hosts: tuple[str, ...] = ()
    ip_literal_attempted: bool = False


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


def append_egress_metric(path: Path, *, hostname: str | None) -> None:
    """Persist a hostname-only child egress observation.

    Network URLs, ports, headers, addresses, and payloads are intentionally
    excluded. A direct IP target is retained only as a boolean so it cannot
    evade the egress gate while still never appearing in the final report.
    """
    safe_hostname = _normalise_hostname(hostname)
    if safe_hostname is not None:
        record: dict[str, object] = {"hostname": safe_hostname}
    elif _is_ip_literal(hostname):
        record = {"ip_literal": True}
    else:
        return
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


def read_egress_hosts(path: Path) -> tuple[str, ...]:
    """Return the sorted hostname-only set from a child egress metric log."""
    return read_egress_observation(path).hosts


def read_egress_observation(path: Path) -> EgressObservation:
    """Return hostnames plus the redacted direct-IP-attempt signal."""
    if not path.is_file():
        return EgressObservation()
    hosts: set[str] = set()
    ip_literal_attempted = False
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        hostname = _normalise_hostname(item.get("hostname"))
        if hostname is not None:
            hosts.add(hostname)
        if item.get("ip_literal") is True:
            ip_literal_attempted = True
    return EgressObservation(hosts=tuple(sorted(hosts)), ip_literal_attempted=ip_literal_attempted)


def _normalise_hostname(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    hostname = value.strip().strip(".").lower()
    if not hostname or len(hostname) > 253 or not _HOSTNAME.fullmatch(hostname):
        return None
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return hostname
    return None


def _is_ip_literal(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return False
    return True
